from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from jaster.agents import build_agents
from jaster.domain import (
    ActionPlan,
    ArtifactRef,
    AvailableTool,
    ChallengeSpec,
    ExecutionResult,
    FailurePattern,
    LatestExecutionResult,
    Observation,
    ObservedTaskResult,
    PlanInput,
    PlannerContext,
    PlannerHistoryEntry,
    PlanningAttempt,
    RecentObservationRound,
    ReflectionHistoryEntry,
    ReflectionInput,
    ReflectionOutput,
    ReflectionTaskUpdate,
    RunState,
    SharedBulletinDigest,
    SharedBulletinEntry,
    SharedFinding,
    StrategicRejection,
    StrategyInput,
    StrategyTaskResult,
    SubmissionInput,
    SubmissionResult,
    TaskDiscovery,
    TaskExecutionResult,
    TaskNodeSnapshot,
    TaskNodeUpdatePatch,
    TaskStatus,
    TaskTree,
    TaskTreePatch,
    TaskTreeSnapshot,
)
from jaster.mcp import call_mcp_tool_sync, tool_inventory
from jaster.runtime.artifacts import filter_available_artifacts
from jaster.runtime.env import env_int
from jaster.runtime.llm import OpenAIChatClient
from jaster.storage.files import FileRunStore

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"


class _StrategyBulletinBoard:
    def __init__(self, seed_entries: list[SharedBulletinEntry] | None = None) -> None:
        self._entries: list[SharedBulletinEntry] = [item.model_copy(deep=True) for item in seed_entries or []]
        self._lock = threading.Lock()
        self._read_cursors: dict[str, int] = {}

    def register_task(self, task_key: str) -> None:
        with self._lock:
            self._read_cursors.setdefault(task_key, len(self._entries))

    def read_for_task(
        self,
        task_key: str,
        *,
        verified_window: int = 12,
        unverified_window: int = 8,
    ) -> SharedBulletinDigest:
        with self._lock:
            cursor = self._read_cursors.setdefault(task_key, len(self._entries))
            new_entries = [
                item.model_copy(deep=True)
                for item in self._entries[cursor:]
                if item.source_task_key != task_key
            ]
            self._read_cursors[task_key] = len(self._entries)
            verified_entries = [
                item.model_copy(deep=True)
                for item in self._entries
                if item.is_verified and item.source_task_key != task_key
            ][-verified_window:]
            unverified_entries = [
                item.model_copy(deep=True)
                for item in self._entries
                if not item.is_verified and item.source_task_key != task_key
            ][-unverified_window:]
        return SharedBulletinDigest(
            new_entries=new_entries,
            verified_entries=verified_entries,
            unverified_entries=unverified_entries,
        )

    def post(
        self,
        *,
        cycle: int,
        source_task_key: str,
        source_task_title: str,
        source_strategy_round: int,
        finding: SharedFinding,
        is_verified: bool = False,
    ) -> SharedBulletinEntry | None:
        if not is_verified and finding.confidence < 0.5:
            return None

        with self._lock:
            for index, existing in enumerate(self._entries):
                if not _shared_bulletin_matches(
                    existing,
                    source_task_key=source_task_key,
                    category=finding.category,
                    title=finding.title,
                    content=finding.content,
                ):
                    continue
                updated = existing.model_copy(
                    update={
                        "cycle": max(existing.cycle, cycle),
                        "source_task_title": source_task_title or existing.source_task_title,
                        "source_strategy_round": max(existing.source_strategy_round, source_strategy_round),
                        "confidence": max(existing.confidence, finding.confidence),
                        "is_verified": existing.is_verified or is_verified,
                    }
                )
                self._entries[index] = updated
                return updated.model_copy(deep=True)

            entry = SharedBulletinEntry(
                entry_id=_shared_bulletin_entry_id(
                    source_task_key=source_task_key,
                    source_strategy_round=source_strategy_round,
                    category=finding.category,
                    title=finding.title,
                    content=finding.content,
                ),
                cycle=cycle,
                source_task_key=source_task_key,
                source_task_title=source_task_title,
                source_strategy_round=source_strategy_round,
                category=finding.category,
                title=finding.title,
                content=finding.content,
                confidence=finding.confidence,
                is_verified=is_verified,
            )
            self._entries.append(entry)
            return entry.model_copy(deep=True)

    def snapshot(self) -> list[SharedBulletinEntry]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._entries]


class JasterOrchestrator:
    def __init__(
        self,
        *,
        store: FileRunStore,
        prompt_root: Path,
        skills_dir: Path,
        llm: OpenAIChatClient,
        verbose: bool = True,
        on_tree_update: Callable[[TaskTreeSnapshot], None] | None = None,
    ) -> None:
        self.store = store
        self.prompt_root = prompt_root
        self.skills_dir = skills_dir
        self.agents = build_agents(prompt_root, llm)
        self.verbose = verbose
        self.phase_max_retries = env_int("JASTER_PHASE_MAX_RETRIES", 3)
        self.parallel_task_workers = env_int("JASTER_PARALLEL_TASK_WORKERS", 4)
        self.parallel_action_workers = env_int("JASTER_PARALLEL_ACTION_WORKERS", 4)
        self.strategy_max_rounds = env_int("JASTER_STRATEGY_MAX_ROUNDS", 8)
        self.strategy_observation_limit = env_int("JASTER_STRATEGY_RECENT_OBSERVATION_LIMIT", 8)
        self.default_tool_timeout = env_int("JASTER_MCP_TOOL_TIMEOUT", 180)
        self.planner_context_window = env_int("JASTER_PLANNER_CONTEXT_WINDOW", 8)
        self._on_tree_update = on_tree_update

    def run(
        self,
        challenge: ChallengeSpec,
        *,
        max_rounds: int = 12,
        submission_handler: callable | None = None,
        round_hook: callable | None = None,
    ) -> RunState:
        run_id = self.store.new_run_id()
        task_tree = TaskTree.bootstrap(challenge.target)
        initial_snapshot = task_tree.snapshot()
        state = RunState(
            run_id=run_id,
            challenge=challenge,
            task_tree=initial_snapshot,
            planner_context=_initial_planner_context(challenge, initial_snapshot),
        )
        self.store.create(state)
        self._log(f"[*] Run created: {run_id}")
        self._log(f"[*] Target: {challenge.target} | type={challenge.target_type} | zone={challenge.zone}")
        self._log(f"[*] Run dir: {self.store.run_dir(run_id)}")

        bootstrap_execution = self._initial_bootstrap_execution(challenge)
        if bootstrap_execution and bootstrap_execution.artifacts:
            state.available_artifacts = _merge_artifact_refs(state.available_artifacts, bootstrap_execution.artifacts)

        for cycle in range(1, max_rounds + 1):
            if self._halt_requested(run_id):
                self._log("[*] Run stopping: halt signal detected")
                break

            self._log(f"[*] Cycle {cycle}: plan")
            previous_keys = {node.key for node in state.task_tree.nodes}
            plan_out, plan_elapsed = self._timed_agent_run(
                "plan",
                challenge.zone,
                PlanInput(
                    objective=f"Plan the next batch of penetration tasks for {challenge.target}.",
                    task_tree=_prompt_task_tree(state.task_tree),
                    challenge_context=_challenge_context(challenge),
                    bootstrap_execution=_compact_execution(bootstrap_execution),
                    planner_context=state.planner_context.model_copy(deep=True) if state.planner_context else None,
                    task_status_summary=_task_status_summary(state.task_tree),
                    failure_patterns_summary=_failure_patterns_summary(state.reflection_history),
                    reflection_history=list(state.reflection_history[-8:]),
                    latest_discoveries=_prompt_discoveries(state.latest_discoveries),
                    available_artifacts=_prompt_artifacts(state.available_artifacts),
                ),
            )
            self._log(f"    LLM time: {plan_elapsed:.2f}s")
            task_tree.apply_patch(plan_out.tree_patch)
            state.task_tree = task_tree.snapshot()
            added_keys = [node.key for node in state.task_tree.nodes if node.key not in previous_keys]

            dispatch_keys = self._resolve_dispatch_keys(task_tree, plan_out.dispatch_task_keys)
            dispatch_keys = self._merge_auto_dispatch_keys(task_tree, dispatch_keys, added_keys)
            self._log_plan_cycle(cycle, task_tree, plan_out, dispatch_keys)
            planner_entry = PlannerHistoryEntry(
                cycle=cycle,
                summary=plan_out.phase_summary,
                planner_notes=plan_out.planner_notes,
                dispatched_task_keys=dispatch_keys,
                added_task_titles=[node.title for node in getattr(plan_out.tree_patch, "add_nodes", []) or []],
                continued_task_keys=list(dispatch_keys),
                planning_thought=plan_out.planning_thought.model_copy(deep=True) if plan_out.planning_thought else None,
            )
            state.planner_history.append(planner_entry)
            state.planner_context = _update_planner_context_after_plan(
                state.planner_context,
                planner_entry,
                window=self.planner_context_window,
            )
            self.store.append_round(
                run_id,
                f"plan_round_{cycle:03d}",
                {
                    "cycle": cycle,
                    "agent": "plan",
                    "input": _agent_trace(self.agents.get("plan")),
                    "output": plan_out.model_dump(),
                },
            )
            self.store.save_state(state)
            self._notify_tree_update(state.task_tree)

            if not dispatch_keys:
                in_progress = [node.key for node in state.task_tree.nodes if node.status == TaskStatus.in_progress]
                if not in_progress:
                    self._log("[*] Run stopping: planner dispatched no tasks and no in-progress tasks remain")
                    state.rounds_completed = cycle - 1
                    self.store.save_state(state)
                    self._notify_tree_update(state.task_tree)
                    break
                dispatch_keys = in_progress

            self._log(f"[*] Cycle {cycle}: strategy batch | tasks={len(dispatch_keys)}")
            strategy_results, observations, batch_discoveries, batch_execution, bulletin_board = self._run_strategy_batch(
                run_id=run_id,
                cycle=cycle,
                challenge=challenge,
                task_tree=state.task_tree,
                task_keys=dispatch_keys,
                reflection_history=state.reflection_history,
                available_artifacts=state.available_artifacts,
                observations=state.observations,
                shared_bulletin=state.shared_bulletin,
            )

            state.observations.extend(observations)
            state.latest_discoveries = _merge_discoveries(state.latest_discoveries, batch_discoveries)
            state.available_artifacts = _merge_artifact_refs(
                state.available_artifacts,
                [artifact for result in strategy_results for artifact in result.artifacts],
            )
            state.shared_bulletin = bulletin_board.snapshot()
            self._increment_attempt_counts(task_tree, dispatch_keys)
            state.task_tree = task_tree.snapshot()

            reflection_out, reflection_elapsed = self._timed_agent_run(
                "reflection",
                challenge.zone,
                ReflectionInput(
                    objective="Review the latest strategy batch, update task states, and advise the next planning cycle.",
                    task_tree=_prompt_task_tree(state.task_tree),
                    challenge_context=_challenge_context(challenge),
                    strategy_results=strategy_results,
                    reflection_history=list(state.reflection_history),
                    latest_discoveries=_prompt_discoveries(batch_discoveries),
                    available_artifacts=_prompt_artifacts(state.available_artifacts),
                ),
            )
            self._log(f"[*] Cycle {cycle}: reflection")
            self._log(f"    LLM time: {reflection_elapsed:.2f}s")
            self._log_reflection_cycle(reflection_out)
            self._apply_reflection_updates(task_tree, reflection_out.task_updates)
            state.task_tree = task_tree.snapshot()
            reflection_entry = ReflectionHistoryEntry(
                cycle=cycle,
                summary=reflection_out.summary,
                planner_guidance=reflection_out.planner_guidance,
                task_updates=list(reflection_out.task_updates),
                failure_patterns=list(reflection_out.failure_patterns),
                strategic_rejections=list(reflection_out.strategic_rejections),
                critical_findings=list(reflection_out.critical_findings),
            )
            state.reflection_history.append(reflection_entry)
            self._publish_reflection_bulletins(
                bulletin_board=bulletin_board,
                cycle=cycle,
                task_tree=state.task_tree,
                reflection_out=reflection_out,
            )
            state.shared_bulletin = bulletin_board.snapshot()
            state.planner_context = _update_planner_context_after_reflection(
                state.planner_context,
                state.task_tree,
                reflection_out,
                window=self.planner_context_window,
            )
            state.latest_discoveries = _merge_discoveries(
                state.latest_discoveries,
                [
                    TaskDiscovery(
                        cycle=cycle,
                        task_key=update.key,
                        task_title=task_tree.get(update.key).title if task_tree.get(update.key) else update.key,
                        source="reflection",
                        summary=update.latest_summary,
                        findings=list(update.latest_findings),
                        credentials=list(reflection_out.credentials),
                        flag_candidates=list(reflection_out.flag_candidates),
                    )
                    for update in reflection_out.task_updates
                    if update.latest_summary or update.latest_findings
                ]
                + (
                    [
                        TaskDiscovery(
                            cycle=cycle,
                            task_key="__reflection__",
                            task_title="Reflection",
                            source="reflection",
                            summary=reflection_out.summary,
                            findings=list(reflection_out.critical_findings),
                            credentials=list(reflection_out.credentials),
                            flag_candidates=list(reflection_out.flag_candidates),
                        )
                    ]
                    if reflection_out.summary or reflection_out.critical_findings
                    else []
                ),
            )
            self.store.append_round(
                run_id,
                f"reflection_round_{cycle:03d}",
                {
                    "cycle": cycle,
                    "agent": "reflection",
                    "input": _agent_trace(self.agents.get("reflection")),
                    "output": reflection_out.model_dump(),
                    "strategy_results": [item.model_dump() for item in strategy_results],
                },
            )

            candidates = _merge_flag_candidates(
                *[result.flag_candidates for result in strategy_results],
                reflection_out.flag_candidates,
            )
            if candidates:
                submission_out, submission_elapsed = self._timed_agent_run(
                    "submission",
                    challenge.zone,
                    SubmissionInput(
                        candidates=candidates,
                        latest_discoveries=_prompt_discoveries(state.latest_discoveries),
                        submitted_flags=state.submitted_flags,
                    ),
                )
                self._log(f"[*] Cycle {cycle}: submission | LLM time: {submission_elapsed:.2f}s")
                if submission_out.should_submit and submission_out.flag and submission_out.flag not in state.submitted_flags:
                    if submission_handler:
                        submission_result = submission_handler(challenge, submission_out.flag, state)
                        if submission_result.correct:
                            state.submitted_flags.append(submission_out.flag)
                            challenge.flag_count = submission_result.flag_count or challenge.flag_count
                            challenge.flag_got_count = submission_result.flag_got_count or challenge.flag_got_count
                    else:
                        state.submitted_flags.append(submission_out.flag)
                self.store.append_round(
                    run_id,
                    f"submission_round_{cycle:03d}",
                    {
                        "cycle": cycle,
                        "agent": "submission",
                        "input": _agent_trace(self.agents.get("submission")),
                        "output": submission_out.model_dump(),
                    },
                )

            state.rounds_completed = cycle
            self.store.save_state(state)
            self._notify_tree_update(state.task_tree)

            if round_hook and round_hook(state, "cycle", batch_execution):
                self._log("[*] Run stopping: requested by round hook")
                break
            if self._halt_requested(run_id):
                self._log("[*] Run stopping: complete_mission or halt tool triggered")
                break
            if challenge.flag_count > 0 and challenge.flag_got_count >= challenge.flag_count:
                self._log("[*] Run stopping: all flags submitted")
                break

        self.store.save_state(state)
        self._notify_tree_update(state.task_tree)
        self._log(
            f"[*] Run finished: rounds={state.rounds_completed} | submitted_flags={len(state.submitted_flags)}"
        )
        return state

    def _run_strategy_batch(
        self,
        *,
        run_id: str,
        cycle: int,
        challenge: ChallengeSpec,
        task_tree: TaskTreeSnapshot,
        task_keys: list[str],
        reflection_history: list[ReflectionHistoryEntry],
        available_artifacts: list[ArtifactRef],
        observations: list[Observation],
        shared_bulletin: list[SharedBulletinEntry],
    ) -> tuple[list[StrategyTaskResult], list[Observation], list[TaskDiscovery], ExecutionResult | None, _StrategyBulletinBoard]:
        nodes_by_key = {node.key: node for node in task_tree.nodes}
        valid_keys = [key for key in task_keys if key in nodes_by_key]
        bulletin_board = _StrategyBulletinBoard(seed_entries=shared_bulletin)
        if not valid_keys:
            return [], [], [], None, bulletin_board

        results: list[StrategyTaskResult] = []
        collected_observations: list[Observation] = []
        discoveries: list[TaskDiscovery] = []

        workers = max(1, min(self.parallel_task_workers, len(valid_keys)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    self._run_strategy_task,
                    run_id=run_id,
                    cycle=cycle,
                    challenge=challenge,
                    task_tree=task_tree,
                    task_node=nodes_by_key[key],
                    reflection_history=reflection_history,
                    available_artifacts=available_artifacts,
                    observations=observations,
                    bulletin_board=bulletin_board,
                ): key
                for key in valid_keys
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    result, task_observations = future.result()
                except Exception as exc:
                    node = nodes_by_key[key]
                    result = StrategyTaskResult(
                        task_key=node.key,
                        task_title=node.title,
                        completed=False,
                        rounds_used=0,
                        termination_reason="strategy_error",
                        phase_summary=f"Strategy execution failed: {exc}",
                        task_summary="",
                        task_findings=[str(exc)],
                    )
                    task_observations = []
                results.append(result)
                collected_observations.extend(task_observations)
                discoveries.append(
                    TaskDiscovery(
                        cycle=cycle,
                        task_key=result.task_key,
                        task_title=result.task_title,
                        source="strategy",
                        summary=result.task_summary or result.phase_summary,
                        findings=list(result.task_findings),
                        flag_candidates=list(result.flag_candidates),
                        credentials=list(result.credentials),
                    )
                )
                self.store.append_round(
                    run_id,
                    f"strategy_batch_{cycle:03d}_{key}",
                    {
                        "cycle": cycle,
                        "task_key": key,
                        "result": result.model_dump(),
                    },
                )

        results.sort(key=lambda item: item.task_key)
        return results, collected_observations, discoveries, _aggregate_strategy_execution(results), bulletin_board

    def _run_strategy_task(
        self,
        *,
        run_id: str,
        cycle: int,
        challenge: ChallengeSpec,
        task_tree: TaskTreeSnapshot,
        task_node: TaskNodeSnapshot,
        reflection_history: list[ReflectionHistoryEntry],
        available_artifacts: list[ArtifactRef],
        observations: list[Observation],
        bulletin_board: _StrategyBulletinBoard,
    ) -> tuple[StrategyTaskResult, list[Observation]]:
        recent_rounds = _recent_observations_for_task(
            observations,
            task_key=task_node.key,
            limit=self.strategy_observation_limit,
        )
        bulletin_board.register_task(task_node.key)
        self._log(f"    [task] {task_node.key} | {task_node.title}")
        latest_execution: ExecutionResult | None = None
        collected: list[Observation] = []
        last_output = None
        termination_reason = "finish"
        rounds_used = 0

        for strategy_round in range(1, self.strategy_max_rounds + 1):
            if self._halt_requested(run_id):
                termination_reason = "halt"
                break
            rounds_used = strategy_round
            self._log(f"      [strategy-round] {task_node.key} #{strategy_round}")
            bulletin_digest = bulletin_board.read_for_task(task_node.key)
            self._log_bulletin_injection(
                task_key=task_node.key,
                strategy_round=strategy_round,
                digest=bulletin_digest,
            )

            try:
                strategy_out, _ = self._timed_agent_run(
                    "strategy",
                    challenge.zone,
                    StrategyInput(
                        objective=f"Complete assigned task: {task_node.title}",
                        assigned_task=task_node,
                        task_tree=_prompt_task_tree(task_tree),
                        challenge_context=_challenge_context(challenge),
                        recent_observations=list(recent_rounds),
                        latest_execution=_compact_execution(latest_execution),
                        reflection_history=reflection_history[-8:],
                        available_artifacts=_prompt_artifacts(available_artifacts),
                        available_tools=_available_tools(),
                        shared_bulletin=bulletin_digest,
                    ),
                )
            except Exception as exc:
                termination_reason = "strategy_error"
                last_output = self.agents["strategy"].output_model(
                    phase_summary=f"Strategy agent failed: {exc}",
                    is_complete=False,
                    task_summary="",
                    task_findings=[str(exc)],
                    actions=[ActionPlan(task_id="finish", kind="finish", goal="Stop current task.")],
                    flag_candidates=[],
                    observed_task_results=[],
                    credentials=[],
                    shared_findings=[],
                )
                break
            last_output = strategy_out
            self._publish_strategy_shared_findings(
                bulletin_board=bulletin_board,
                cycle=cycle,
                task_node=task_node,
                strategy_round=strategy_round,
                shared_findings=list(strategy_out.shared_findings),
            )

            if strategy_out.is_complete:
                termination_reason = "completed"
                break

            actions = strategy_out.actions
            if _is_finish_only(actions):
                termination_reason = "finish"
                break

            latest_execution = self._execute_actions(
                run_id=run_id,
                task_key=task_node.key,
                cycle=cycle,
                strategy_round=strategy_round,
                actions=actions,
            )
            observed = _normalize_observed_task_results(strategy_out.observed_task_results, latest_execution.task_results)
            collected.extend(
                _observations_from_execution(
                    cycle=cycle,
                    strategy_round=strategy_round,
                    task_node=task_node,
                    latest_execution=latest_execution,
                    observed=observed,
                )
            )
            recent_rounds = _serialize_recent_observations(collected, limit=self.strategy_observation_limit)

        if last_output is None:
            last_output = self.agents["strategy"].output_model(
                phase_summary="Strategy did not produce any output.",
                is_complete=False,
                task_summary="",
                task_findings=[],
                actions=[ActionPlan(task_id="finish", kind="finish", goal="Stop current task.")],
                flag_candidates=[],
                observed_task_results=[],
                credentials=[],
                shared_findings=[],
            )

        if not last_output.is_complete and termination_reason == "finish" and rounds_used >= self.strategy_max_rounds:
            termination_reason = "max_rounds"

        result = StrategyTaskResult(
            task_key=task_node.key,
            task_title=task_node.title,
            completed=last_output.is_complete,
            rounds_used=rounds_used,
            termination_reason=termination_reason,
            phase_summary=last_output.phase_summary,
            task_summary=last_output.task_summary,
            task_findings=list(last_output.task_findings),
            flag_candidates=list(last_output.flag_candidates),
            credentials=list(last_output.credentials),
            latest_execution=_compact_execution(latest_execution),
            observed_task_results=list(last_output.observed_task_results),
            artifacts=list(latest_execution.artifacts) if latest_execution else [],
        )
        self._log_task_result(result)
        return result, collected

    def _execute_actions(
        self,
        *,
        run_id: str,
        task_key: str,
        cycle: int,
        strategy_round: int,
        actions: list[ActionPlan],
    ) -> ExecutionResult:
        if not actions:
            return ExecutionResult(success=False, summary="No actions to execute", failure_stage="action_execution")

        if _is_finish_only(actions):
            finish_action = actions[0]
            task_result = TaskExecutionResult(
                task_id=finish_action.task_id,
                kind="finish",
                success=True,
                summary=finish_action.goal,
                source="strategy",
            )
            return _build_batch_execution("strategy", [task_result])

        task_results: list[TaskExecutionResult] = []
        workers = max(1, min(self.parallel_action_workers, len(actions)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    self._execute_single_action,
                    run_id=run_id,
                    task_key=task_key,
                    cycle=cycle,
                    strategy_round=strategy_round,
                    action=action,
                ): action
                for action in actions
            }
            for future in as_completed(future_map):
                task_results.append(future.result())

        task_results.sort(key=lambda item: item.task_id)
        return _build_batch_execution("strategy", task_results)

    def _execute_single_action(
        self,
        *,
        run_id: str,
        task_key: str,
        cycle: int,
        strategy_round: int,
        action: ActionPlan,
    ) -> TaskExecutionResult:
        if action.kind == "finish":
            return TaskExecutionResult(
                task_id=action.task_id,
                kind="finish",
                success=True,
                summary=action.goal,
                source="strategy",
            )

        tool_name = action.tool_name or ""
        tool_args = _strip_none_values(dict(action.tool_args or {}))
        if tool_name == "complete_mission" and "task_id" not in tool_args:
            tool_args["task_id"] = run_id
        tool_timeout = _tool_timeout(tool_name, default=self.default_tool_timeout)
        started = time.monotonic()
        self._log_action_start(
            task_key=task_key,
            strategy_round=strategy_round,
            action=action,
        )
        try:
            payload = call_mcp_tool_sync(tool_name, tool_args, timeout=tool_timeout)
            result = _normalize_mcp_execution_result(
                tool_name=tool_name,
                tool_args=tool_args,
                result_payload=payload,
                source="strategy",
            )
            result.command = f"{tool_name} {json.dumps(tool_args, ensure_ascii=False)}"
        except Exception as exc:
            result = ExecutionResult(
                success=False,
                summary=f"MCP call failed: {exc}",
                stderr=str(exc),
                exit_code=1,
                command=f"{tool_name} {json.dumps(tool_args, ensure_ascii=False)}",
                source="strategy",
                failure_stage="mcp_execution",
            )
        self._log_action_result(
            task_key=task_key,
            strategy_round=strategy_round,
            action=action,
            execution=result,
            elapsed=time.monotonic() - started,
        )
        return _to_task_result(
            action=action,
            execution=result,
            source="strategy",
            task_key=task_key,
            elapsed=time.monotonic() - started,
        )

    def _publish_strategy_shared_findings(
        self,
        *,
        bulletin_board: _StrategyBulletinBoard,
        cycle: int,
        task_node: TaskNodeSnapshot,
        strategy_round: int,
        shared_findings: list[SharedFinding],
    ) -> None:
        for finding in shared_findings:
            posted = bulletin_board.post(
                cycle=cycle,
                source_task_key=task_node.key,
                source_task_title=task_node.title,
                source_strategy_round=strategy_round,
                finding=finding,
                is_verified=False,
            )
            if posted:
                self._log_bulletin_post(task_key=task_node.key, strategy_round=strategy_round, entry=posted)

    def _publish_reflection_bulletins(
        self,
        *,
        bulletin_board: _StrategyBulletinBoard,
        cycle: int,
        task_tree: TaskTreeSnapshot,
        reflection_out: ReflectionOutput,
    ) -> None:
        titles = {node.key: node.title for node in task_tree.nodes}
        for update in reflection_out.task_updates:
            source_title = titles.get(update.key, update.key)
            for finding_text in update.latest_findings:
                posted = bulletin_board.post(
                    cycle=cycle,
                    source_task_key=update.key,
                    source_task_title=source_title,
                    source_strategy_round=0,
                    finding=SharedFinding(
                        category="verified_task_finding",
                        title=_excerpt(finding_text, limit=80),
                        content=finding_text,
                        confidence=1.0,
                    ),
                    is_verified=True,
                )
                if posted:
                    self._log_bulletin_verified(source=f"{update.key} | {source_title}", entry=posted)
        for finding_text in reflection_out.critical_findings:
            posted = bulletin_board.post(
                cycle=cycle,
                source_task_key="__reflection__",
                source_task_title="Reflection",
                source_strategy_round=0,
                finding=SharedFinding(
                    category="reflection_critical",
                    title=_excerpt(finding_text, limit=80),
                    content=finding_text,
                    confidence=1.0,
                ),
                is_verified=True,
            )
            if posted:
                self._log_bulletin_verified(source="reflection", entry=posted)

    def _increment_attempt_counts(self, task_tree: TaskTree, task_keys: list[str]) -> None:
        updates = []
        for key in task_keys:
            node = task_tree.get(key)
            if node is None:
                continue
            updates.append(TaskNodeUpdatePatch(key=key, attempt_count=node.attempt_count + 1))
        if updates:
            task_tree.apply_patch(TaskTreePatch(update_nodes=updates))

    def _apply_reflection_updates(self, task_tree: TaskTree, updates: list[ReflectionTaskUpdate]) -> None:
        patch = TaskTreePatch(
            update_nodes=[
                TaskNodeUpdatePatch(
                    key=item.key,
                    status=item.status,
                    latest_summary=item.latest_summary,
                    latest_findings=list(item.latest_findings),
                )
                for item in updates
            ]
        )
        task_tree.apply_patch(patch)

    def _resolve_dispatch_keys(self, task_tree: TaskTree, task_keys: list[str]) -> list[str]:
        child_keys = {node.parent_key for node in task_tree.snapshot().nodes if node.parent_key}
        seen: list[str] = []
        for key in task_keys:
            node = task_tree.get(key)
            if (
                node is None
                or node.status != TaskStatus.in_progress
                or key in child_keys
                or key in seen
            ):
                continue
            seen.append(key)
        return seen

    def _merge_auto_dispatch_keys(self, task_tree: TaskTree, dispatch_keys: list[str], added_keys: list[str]) -> list[str]:
        child_keys = {node.parent_key for node in task_tree.snapshot().nodes if node.parent_key}
        auto_keys: list[str] = []
        for key in added_keys:
            node = task_tree.get(key)
            if node is None or node.status != TaskStatus.in_progress or key in child_keys:
                continue
            auto_keys.append(key)

        if not auto_keys:
            return dispatch_keys

        merged = list(auto_keys)
        for key in dispatch_keys:
            if key not in merged:
                merged.append(key)
        return merged

    def _initial_bootstrap_execution(self, challenge: ChallengeSpec) -> ExecutionResult | None:
        if challenge.target_type != "http":
            return None
        self._log(f"[*] Initial curl: {challenge.target}")
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "30", challenge.target],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return ExecutionResult(
            success=result.returncode == 0,
            summary="Initial HTTP response captured" if result.stdout else "",
            findings=[],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            command=f"curl -s -L {challenge.target}",
            source="bootstrap",
            failure_stage="" if result.returncode == 0 else "bootstrap",
        )

    def _halt_requested(self, run_id: str) -> bool:
        return Path(tempfile.gettempdir(), f"{run_id}.halt").exists()

    def _timed_agent_run(
        self,
        agent_name: str,
        zone: str,
        payload: object,
        *,
        retry_context: dict[str, Any] | None = None,
    ) -> tuple[object, float]:
        started = time.monotonic()
        agent = self.agents[agent_name]
        current_retry_context = dict(retry_context or {})
        attempts = max(1, self.phase_max_retries)
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                output = agent.run(zone, payload, retry_context=current_retry_context or None)
                return output, time.monotonic() - started
            except Exception as exc:
                last_exc = exc
                current_retry_context = {
                    "agent": agent_name,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
                self._log(f"[!] Agent {agent_name} attempt {attempt}/{attempts} failed: {exc}")
                if attempt >= attempts:
                    break

        assert last_exc is not None
        raise last_exc

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def _notify_tree_update(self, tree_snapshot: TaskTreeSnapshot) -> None:
        if self._on_tree_update:
            self._on_tree_update(tree_snapshot)

    def _log_plan_cycle(
        self,
        cycle: int,
        task_tree: TaskTree,
        plan_out: object,
        dispatch_keys: list[str],
    ) -> None:
        summary = getattr(plan_out, "phase_summary", "")
        planner_notes = getattr(plan_out, "planner_notes", "")
        planning_thought = getattr(plan_out, "planning_thought", None)
        tree_patch = getattr(plan_out, "tree_patch", None)
        add_nodes = list(getattr(tree_patch, "add_nodes", []) or [])
        update_nodes = list(getattr(tree_patch, "update_nodes", []) or [])

        if summary:
            self._log(f"    {_style('[plan:summary]', ANSI_CYAN, bold=True)} {_excerpt(summary, limit=240)}")
        if planner_notes:
            self._log(f"    {_style('[plan:notes]', ANSI_DIM)} {_excerpt(planner_notes, limit=240)}")
        if planning_thought:
            analysis = str(getattr(planning_thought, "analysis", "") or "")
            failure_diagnosis = str(getattr(planning_thought, "failure_diagnosis", "") or "")
            decomposition = str(getattr(planning_thought, "decomposition", "") or "")
            dispatch_rationale = str(getattr(planning_thought, "dispatch_rationale", "") or "")
            if analysis:
                self._log(f"    {_style('[plan:analysis]', ANSI_DIM)} {_excerpt(analysis, limit=240)}")
            if failure_diagnosis:
                self._log(f"    {_style('[plan:failure]', ANSI_DIM)} {_excerpt(failure_diagnosis, limit=240)}")
            if decomposition:
                self._log(f"    {_style('[plan:decompose]', ANSI_DIM)} {_excerpt(decomposition, limit=240)}")
            if dispatch_rationale:
                self._log(f"    {_style('[plan:dispatch-note]', ANSI_DIM)} {_excerpt(dispatch_rationale, limit=240)}")
        for index, node in enumerate(add_nodes, start=1):
            self._log(
                "    "
                + _style(f"[plan:add:{index}]", ANSI_CYAN, bold=True)
                + f" {node.title} | reason={_excerpt(node.reason, limit=120)} | done={_excerpt(node.completion_criteria, limit=120)}"
            )
        for item in update_nodes:
            status = getattr(item, "status", None)
            self._log(
                "    "
                + _style("[plan:update]", ANSI_CYAN)
                + f" {item.key} | status={status or '-'} | summary={_excerpt(str(getattr(item, 'latest_summary', '') or ''), limit=120)}"
            )
        for key in dispatch_keys:
            node = task_tree.get(key)
            title = node.title if node else key
            self._log("    " + _style("[plan:dispatch]", ANSI_BLUE, bold=True) + f" {key} | {title}")

    def _log_reflection_cycle(self, reflection_out: ReflectionOutput) -> None:
        if reflection_out.summary:
            self._log(
                f"    {_style('[reflection:summary]', ANSI_MAGENTA, bold=True)} {_excerpt(reflection_out.summary, limit=240)}"
            )
        if reflection_out.planner_guidance:
            self._log(
                f"    {_style('[reflection:guidance]', ANSI_DIM)} {_excerpt(reflection_out.planner_guidance, limit=240)}"
            )
        for item in reflection_out.failure_patterns[:4]:
            self._log(
                "    "
                + _style("[reflection:pattern]", ANSI_YELLOW, bold=True)
                + f" {item.pattern} | reason={_excerpt(item.reason, limit=160)}"
            )
        for item in reflection_out.strategic_rejections[:4]:
            self._log(
                "    "
                + _style("[reflection:reject]", ANSI_RED, bold=True)
                + f" {item.label} | reason={_excerpt(item.reason, limit=160)}"
            )
        for finding in reflection_out.critical_findings[:4]:
            self._log("    " + _style("[reflection:critical]", ANSI_MAGENTA) + f" {_excerpt(finding, limit=180)}")

    def _log_action_start(
        self,
        *,
        task_key: str,
        strategy_round: int,
        action: ActionPlan,
    ) -> None:
        goal = _excerpt(action.goal, limit=180)
        tool_name = action.tool_name or action.kind
        self._log(
            "        "
            + _style("[action:start]", ANSI_BLUE, bold=True)
            + f" {task_key}#{strategy_round} {action.task_id} -> {tool_name}"
            + f" | goal={goal}"
        )

    def _log_action_result(
        self,
        *,
        task_key: str,
        strategy_round: int,
        action: ActionPlan,
        execution: ExecutionResult,
        elapsed: float,
    ) -> None:
        tone = ANSI_GREEN if execution.success else ANSI_RED
        label = "[action:ok]" if execution.success else "[action:fail]"
        details = _excerpt(execution.summary or execution.stderr or execution.stdout, limit=220)
        self._log(
            "        "
            + _style(label, tone, bold=True)
            + f" {task_key}#{strategy_round} {action.task_id}"
            + f" | {elapsed:.2f}s | {details}"
        )

    def _log_task_result(self, result: StrategyTaskResult) -> None:
        if result.completed:
            tone = ANSI_GREEN
            label = "[task:completed]"
        elif result.termination_reason in {"strategy_error", "max_rounds"}:
            tone = ANSI_RED
            label = "[task:stopped]"
        else:
            tone = ANSI_YELLOW
            label = "[task:paused]"
        summary = _excerpt(result.task_summary or result.phase_summary, limit=220)
        self._log(
            "    "
            + _style(label, tone, bold=True)
            + f" {result.task_key} | rounds={result.rounds_used} | reason={result.termination_reason or '-'} | {summary}"
        )

    def _log_bulletin_injection(
        self,
        *,
        task_key: str,
        strategy_round: int,
        digest: SharedBulletinDigest,
    ) -> None:
        self._log(
            "        "
            + _style("[bulletin:inject]", ANSI_CYAN, bold=True)
            + f" {task_key}#{strategy_round}"
            + f" | new={len(digest.new_entries)}"
            + f" verified={len(digest.verified_entries)}"
            + f" unverified={len(digest.unverified_entries)}"
        )

    def _log_bulletin_post(
        self,
        *,
        task_key: str,
        strategy_round: int,
        entry: SharedBulletinEntry,
    ) -> None:
        self._log(
            "        "
            + _style("[bulletin:post]", ANSI_YELLOW, bold=True)
            + f" {task_key}#{strategy_round}"
            + f" | {entry.category}"
            + f" | {entry.title}"
        )

    def _log_bulletin_verified(self, *, source: str, entry: SharedBulletinEntry) -> None:
        self._log(
            "    "
            + _style("[bulletin:verify]", ANSI_MAGENTA, bold=True)
            + f" {source}"
            + f" | {entry.category}"
            + f" | {_excerpt(entry.content, limit=180)}"
        )


def detect_target_type(target: str) -> str:
    return "http" if target.startswith(("http://", "https://")) else "tcp"


def detect_zone(description: str) -> str:
    lowered = description.lower()
    if any(token in lowered for token in ["kerberos", "domain", "active directory", "ad"]):
        return "zone4"
    if any(token in lowered for token in ["pivot", "multi-step", "内网"]):
        return "zone3"
    if any(token in lowered for token in ["cve", "cloud", "组件"]):
        return "zone2"
    return "zone1"


def _prompt_task_tree(snapshot: TaskTreeSnapshot) -> TaskTreeSnapshot:
    return TaskTreeSnapshot(nodes=list(snapshot.nodes))


def _prompt_artifacts(artifacts: list[ArtifactRef], *, limit: int = 20) -> list[ArtifactRef]:
    filtered = filter_available_artifacts(artifacts)
    return [item.model_copy(deep=True) for item in filtered[-limit:]]


def _prompt_discoveries(discoveries: list[TaskDiscovery], *, limit: int = 20) -> list[TaskDiscovery]:
    return [item.model_copy(deep=True) for item in discoveries[-limit:]]


def _initial_planner_context(challenge: ChallengeSpec, task_tree: TaskTreeSnapshot) -> PlannerContext:
    root = task_tree.nodes[0] if task_tree.nodes else None
    long_term_objectives = []
    if root and root.completion_criteria:
        long_term_objectives.append(root.completion_criteria)
    elif challenge.description:
        long_term_objectives.append(challenge.description)
    return PlannerContext(
        initial_objective=challenge.description or (root.title if root else challenge.target),
        target=challenge.target,
        long_term_objectives=long_term_objectives,
    )


def _task_status_summary(snapshot: TaskTreeSnapshot) -> str:
    in_progress = [node for node in snapshot.nodes if node.status == TaskStatus.in_progress]
    completed = [node for node in snapshot.nodes if node.status == TaskStatus.completed]
    failed = [node for node in snapshot.nodes if node.status == TaskStatus.failed]
    leaf_keys = {node.key for node in snapshot.nodes}
    parent_keys = {node.parent_key for node in snapshot.nodes if node.parent_key}
    leaf_nodes = [node for node in snapshot.nodes if node.key in leaf_keys - parent_keys]

    lines = [
        f"任务统计: in_progress={len(in_progress)}, completed={len(completed)}, failed={len(failed)}, leaf={len(leaf_nodes)}",
    ]
    if in_progress:
        lines.append("进行中叶子任务:")
        for node in [item for item in leaf_nodes if item.status == TaskStatus.in_progress][:6]:
            lines.append(
                f"- {node.key} | {node.title} | attempts={node.attempt_count} | summary={_excerpt(node.latest_summary, limit=100)}"
            )
    if failed:
        lines.append("近期失败任务:")
        for node in failed[-6:]:
            lines.append(f"- {node.key} | {node.title} | summary={_excerpt(node.latest_summary, limit=120)}")
    if completed:
        lines.append("近期完成任务:")
        for node in completed[-4:]:
            lines.append(f"- {node.key} | {node.title} | summary={_excerpt(node.latest_summary, limit=100)}")
    return "\n".join(lines)


def _failure_patterns_summary(reflection_history: list[ReflectionHistoryEntry]) -> str:
    if not reflection_history:
        return "暂无失败模式摘要"
    patterns: list[str] = []
    for entry in reflection_history[-5:]:
        for item in entry.failure_patterns:
            affected = f" | tasks={','.join(item.affected_task_keys[:3])}" if item.affected_task_keys else ""
            patterns.append(f"- cycle={entry.cycle} | {item.pattern} | {item.reason}{affected}")
    if not patterns:
        return "暂无失败模式摘要"
    return "\n".join(patterns[:12])


def _update_planner_context_after_plan(
    context: PlannerContext | None,
    planner_entry: PlannerHistoryEntry,
    *,
    window: int,
) -> PlannerContext | None:
    if context is None:
        return None
    context = context.model_copy(deep=True)
    context.planning_attempts.append(
        PlanningAttempt(
            cycle=planner_entry.cycle,
            phase_summary=planner_entry.summary,
            planner_notes=planner_entry.planner_notes,
            added_task_titles=list(planner_entry.added_task_titles),
            continued_task_keys=list(planner_entry.continued_task_keys),
        )
    )
    return _compress_planner_context(context, window=window)


def _update_planner_context_after_reflection(
    context: PlannerContext | None,
    task_tree: TaskTreeSnapshot,
    reflection_out: ReflectionOutput,
    *,
    window: int,
) -> PlannerContext | None:
    if context is None:
        return None
    context = context.model_copy(deep=True)
    digest_lines = []
    if reflection_out.summary:
        digest_lines.append(reflection_out.summary)
    if reflection_out.planner_guidance:
        digest_lines.append(f"guidance: {reflection_out.planner_guidance}")
    if reflection_out.critical_findings:
        digest_lines.append("critical: " + "; ".join(reflection_out.critical_findings[:4]))
    context.latest_reflection_digest = " | ".join(digest_lines)

    for item in reflection_out.strategic_rejections:
        if item.label and item.label not in context.rejected_strategies:
            context.rejected_strategies[item.label] = item.reason

    if reflection_out.planner_guidance:
        stage_goal = f"阶段主线: {_excerpt(reflection_out.planner_guidance, limit=180)}"
        if stage_goal not in context.long_term_objectives:
            context.long_term_objectives.append(stage_goal)

    root = task_tree.nodes[0] if task_tree.nodes else None
    if root and root.completion_criteria and root.completion_criteria not in context.long_term_objectives:
        context.long_term_objectives.insert(0, root.completion_criteria)

    return _compress_planner_context(context, window=window)


def _compress_planner_context(context: PlannerContext, *, window: int) -> PlannerContext:
    if len(context.planning_attempts) <= window:
        return context

    older_attempts = context.planning_attempts[:-window]
    summary_lines = []
    if context.compressed_history_summary:
        summary_lines.append(context.compressed_history_summary)
    for attempt in older_attempts:
        added = ", ".join(attempt.added_task_titles[:3]) if attempt.added_task_titles else "-"
        continued = ", ".join(attempt.continued_task_keys[:3]) if attempt.continued_task_keys else "-"
        summary_lines.append(
            f"cycle {attempt.cycle}: {attempt.phase_summary} | added={added} | continued={continued}"
        )
    context.compressed_history_summary = "\n".join(line for line in summary_lines if line).strip()
    context.compression_count += 1
    context.planning_attempts = context.planning_attempts[-window:]
    return context


def _compact_execution(execution: ExecutionResult | None) -> LatestExecutionResult | None:
    if execution is None:
        return None
    return LatestExecutionResult(
        success=execution.success,
        batch_status=execution.batch_status,
        summary=_excerpt(execution.summary, limit=1200),
        findings=[_excerpt(item, limit=1600) for item in execution.findings[:20]],
        stdout=_excerpt_head_tail(execution.stdout, limit=50000),
        stderr=_excerpt_head_tail(execution.stderr, limit=50000),
        command=_excerpt(execution.command, limit=1200),
        source=execution.source,
        failure_stage=execution.failure_stage,
        task_results={key: _compact_task_result(value) for key, value in execution.task_results.items()},
    )


def _compact_task_result(task_result: TaskExecutionResult) -> TaskExecutionResult:
    return task_result.model_copy(
        update={
            "summary": _excerpt(task_result.summary, limit=1200),
            "findings": [_excerpt(item, limit=1600) for item in task_result.findings[:20]],
            "stdout": _excerpt_head_tail(task_result.stdout, limit=50000),
            "stderr": _excerpt_head_tail(task_result.stderr, limit=50000),
            "command": _excerpt(task_result.command, limit=1200),
            "script_path": _excerpt(task_result.script_path, limit=600),
        },
        deep=True,
    )


def _available_tools() -> list[AvailableTool]:
    tools: list[AvailableTool] = []
    for item in tool_inventory():
        input_schema = item.get("inputSchema") or {}
        tools.append(
            AvailableTool(
                name=str(item.get("name") or ""),
                summary=str(item.get("summary") or ""),
                server_name=str(item.get("server_name") or ""),
                tool_schema_text=_render_tool_schema_text(item.get("name", ""), input_schema),
                tool_definition_json=json.dumps(input_schema, ensure_ascii=False, indent=2),
            )
        )
    return tools


def _render_tool_schema_text(name: str, schema: dict[str, Any]) -> str:
    lines = [f"name: {name}"]
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if properties:
        lines.append("params:")
        for param_name, param_info in properties.items():
            param_type = param_info.get("type", "any")
            req = "required" if param_name in required else "optional"
            desc = str(param_info.get("description") or "")
            lines.append(f"- {param_name}: {param_type}, {req}, {desc}")
    return "\n".join(lines)


def _shared_bulletin_entry_id(
    *,
    source_task_key: str,
    source_strategy_round: int,
    category: str,
    title: str,
    content: str,
) -> str:
    payload = "|".join(
        [
            source_task_key,
            str(source_strategy_round),
            category.strip(),
            title.strip(),
            content.strip(),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _shared_bulletin_matches(
    entry: SharedBulletinEntry,
    *,
    source_task_key: str,
    category: str,
    title: str,
    content: str,
) -> bool:
    return (
        entry.source_task_key == source_task_key
        and entry.category == category
        and entry.title == title
        and entry.content == content
    )


def _challenge_context(challenge: ChallengeSpec) -> str:
    lines: list[str] = []
    if challenge.title:
        lines.append(f"题目标题: {challenge.title}")
    if challenge.description:
        lines.append(f"题目描述: {challenge.description}")
    if challenge.target:
        lines.append(f"主入口: {challenge.target}")
    if challenge.entrypoints:
        others = [item for item in challenge.entrypoints if item and item != challenge.target]
        if others:
            lines.append("其他入口: " + ", ".join(others))
    if challenge.difficulty:
        lines.append(f"难度: {challenge.difficulty}")
    if challenge.level:
        lines.append(f"赛区: level={challenge.level}, zone={challenge.zone}")
    if challenge.flag_count:
        lines.append(f"Flag进度: {challenge.flag_got_count}/{challenge.flag_count}")
    if challenge.hint_content:
        lines.append(f"平台提示: {challenge.hint_content}")
    return "\n".join(line for line in lines if line).strip()


def _recent_observations_for_task(
    observations: list[Observation],
    *,
    task_key: str,
    limit: int,
) -> list[RecentObservationRound]:
    relevant = [item for item in observations if item.task_key == task_key]
    return _serialize_recent_observations(relevant, limit=limit)


def _serialize_recent_observations(
    observations: list[Observation],
    *,
    limit: int,
) -> list[RecentObservationRound]:
    grouped: dict[int, list[Observation]] = {}
    ordered_rounds: list[int] = []
    for item in observations:
        if item.strategy_round not in grouped:
            grouped[item.strategy_round] = []
            ordered_rounds.append(item.strategy_round)
        grouped[item.strategy_round].append(item)
    recent: list[RecentObservationRound] = []
    for round_num in ordered_rounds[-limit:]:
        recent.append(
            RecentObservationRound(
                round=round_num,
                actions=[
                    {
                        "action_task_id": item.action_task_id,
                        "tool_name": item.tool_name,
                        "target": item.target,
                        "result": item.result,
                        "key_findings": item.key_findings,
                    }
                    for item in grouped[round_num]
                ],
            )
        )
    return recent


def _normalize_observed_task_results(
    observed: list[ObservedTaskResult],
    task_results: dict[str, TaskExecutionResult],
) -> list[ObservedTaskResult]:
    by_id = {item.task_id: item for item in observed if item.task_id}
    normalized: list[ObservedTaskResult] = []
    for task_id, result in task_results.items():
        item = by_id.get(task_id)
        if item is None:
            item = ObservedTaskResult(
                task_id=task_id,
                target=result.tool_name or task_id,
                result=result.summary or _execution_message(_build_batch_execution("strategy", [result])),
                key_findings=(result.findings[0] if result.findings else _excerpt(result.stdout or result.stderr, limit=200)),
            )
        else:
            item = item.model_copy(
                update={
                    "target": item.target or result.tool_name or task_id,
                    "result": item.result
                    or result.summary
                    or _execution_message(_build_batch_execution("strategy", [result])),
                    "key_findings": item.key_findings
                    or (result.findings[0] if result.findings else _excerpt(result.stdout or result.stderr, limit=200)),
                }
            )
        normalized.append(item)
    return normalized


def _observations_from_execution(
    *,
    cycle: int,
    strategy_round: int,
    task_node: TaskNodeSnapshot,
    latest_execution: ExecutionResult,
    observed: list[ObservedTaskResult],
) -> list[Observation]:
    by_id = {item.task_id: item for item in observed}
    observations: list[Observation] = []
    for task_id, task_result in latest_execution.task_results.items():
        item = by_id.get(task_id)
        if item is None:
            continue
        observations.append(
            Observation(
                cycle=cycle,
                strategy_round=strategy_round,
                task_key=task_node.key,
                task_title=task_node.title,
                action_task_id=task_id,
                tool_name=task_result.tool_name or "",
                target=item.target,
                result=item.result,
                key_findings=item.key_findings,
            )
        )
    return observations


def _normalize_mcp_execution_result(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    result_payload: str,
    source: str,
) -> ExecutionResult:
    if not result_payload.strip():
        return ExecutionResult(
            success=False,
            summary=f"Empty MCP response from {tool_name}",
            stderr="Empty MCP response",
            source=source,
            failure_stage="mcp_execution",
        )
    try:
        payload = json.loads(result_payload)
    except json.JSONDecodeError:
        return ExecutionResult(
            success=False,
            summary=f"Invalid JSON from {tool_name}",
            stderr=_excerpt_head_tail(result_payload, limit=3000),
            stdout=_excerpt_head_tail(result_payload, limit=3000),
            source=source,
            failure_stage="mcp_execution",
            command=f"{tool_name} {json.dumps(tool_args, ensure_ascii=False)}",
        )

    success, summary, stdout, stderr, findings = _extract_mcp_fields(tool_name, payload)
    return ExecutionResult(
        success=success,
        summary=summary,
        findings=findings,
        stdout=stdout,
        stderr=stderr,
        exit_code=0 if success else 1,
        command=f"{tool_name} {json.dumps(tool_args, ensure_ascii=False)}",
        source=source,
        failure_stage="" if success else "mcp_execution",
        artifacts=_extract_mcp_artifacts(payload, source=source, tool_name=tool_name),
        flag_candidates=_extract_flag_candidates(payload),
    )


def _extract_mcp_fields(tool_name: str, payload: dict[str, Any]) -> tuple[bool, str, str, str, list[str]]:
    if tool_name == "http_request":
        response = payload.get("response") or {}
        status_code = int(response.get("status_code") or 0)
        content = str(response.get("content") or "")
        summary = _excerpt(content or response.get("reason") or f"HTTP {status_code}", limit=500)
        success = 200 <= status_code < 400
        stderr = "" if success else f"HTTP {status_code}"
        findings = [summary] if summary else []
        return success, summary, content, stderr, findings

    status = str(payload.get("status") or "").lower()
    if status in {"error", "timeout"}:
        return False, str(payload.get("error") or payload.get("message") or status), json.dumps(payload, ensure_ascii=False), str(
            payload.get("error") or payload.get("message") or status
        ), []

    explicit_success = payload.get("success")
    if explicit_success is False:
        error_bits = [
            str(payload.get("error") or "").strip(),
            str(payload.get("message") or "").strip(),
            str(payload.get("fix_suggestion") or "").strip(),
            str(payload.get("error_type") or "").strip(),
        ]
        error = " | ".join(item for item in error_bits if item) or "Tool execution failed"
        stdout = json.dumps(payload, ensure_ascii=False, indent=2)
        return False, error, stdout, error, _extract_findings(payload)

    summary = str(
        payload.get("summary")
        or payload.get("message")
        or payload.get("fix_suggestion")
        or payload.get("report")
        or payload.get("status")
        or tool_name
    )
    stdout = payload.get("output")
    if stdout is None:
        stdout = payload.get("stdout")
    if stdout is None:
        stdout = json.dumps(payload, ensure_ascii=False, indent=2)
    stderr = str(payload.get("stderr") or "")
    findings = _extract_findings(payload)
    success = explicit_success is True or status in {"success", "started", "stopped"} or not stderr
    return success, summary, str(stdout), stderr, findings


def _extract_findings(payload: dict[str, Any]) -> list[str]:
    findings = payload.get("findings")
    if isinstance(findings, list):
        return [str(item) for item in findings[:20] if str(item).strip()]
    if isinstance(payload.get("vulnerabilities"), list):
        return [json.dumps(item, ensure_ascii=False) for item in payload["vulnerabilities"][:10]]
    if isinstance(payload.get("results"), list):
        return [json.dumps(item, ensure_ascii=False) for item in payload["results"][:10]]
    if isinstance(payload.get("requests"), list):
        return [json.dumps(item, ensure_ascii=False) for item in payload["requests"][:10]]
    if isinstance(payload.get("templates"), list):
        return [str(item) for item in payload["templates"][:10]]
    summary = str(payload.get("summary") or payload.get("message") or "")
    return [summary] if summary else []


def _extract_flag_candidates(payload: dict[str, Any]) -> list[str]:
    values = payload.get("flag_candidates") or payload.get("flags") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    candidates: list[str] = []
    for item in values:
        rendered = str(item).strip()
        if rendered and rendered not in candidates:
            candidates.append(rendered)
    return candidates


def _extract_mcp_artifacts(payload: dict[str, Any], *, source: str, tool_name: str) -> list[ArtifactRef]:
    artifacts: list[ArtifactRef] = []
    if isinstance(payload.get("artifacts"), list):
        for item in payload["artifacts"]:
            if not isinstance(item, dict):
                continue
            try:
                artifact = ArtifactRef.model_validate(item)
            except Exception:
                continue
            artifacts.append(artifact.model_copy(update={"producer_phase": source, "producer_tool_name": tool_name}))
    for key in ("path", "exploit_path", "output_path"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if not path.exists():
            continue
        kind = "directory" if path.is_dir() else "file"
        artifacts.append(
            ArtifactRef(
                kind=kind,
                path=str(path.resolve()),
                producer_phase=source,
                producer_tool_name=tool_name,
            )
        )
    return _merge_artifact_refs([], artifacts)


def _strip_none_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_none_values(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none_values(item) for item in value if item is not None]
    return value


def _tool_timeout(tool_name: str, *, default: int) -> int:
    overrides = {
        "sqlmap_tool": env_int("JASTER_TIMEOUT_SQLMAP", 600),
        "dirsearch_scan": env_int("JASTER_TIMEOUT_DIRSEARCH", 300),
        "nuclei_scan": env_int("JASTER_TIMEOUT_NUCLEI", 300),
        "python_exec": env_int("JASTER_TIMEOUT_PYTHON_EXEC", 300),
        "concurrency_test": env_int("JASTER_TIMEOUT_CONCURRENCY", 180),
        "http_request": env_int("JASTER_TIMEOUT_HTTP_REQUEST", 60),
        "web_search": env_int("JASTER_TIMEOUT_WEB_SEARCH", 30),
        "expert_analysis": env_int("JASTER_TIMEOUT_EXPERT_ANALYSIS", 90),
    }
    return overrides.get(tool_name, default)


def _describe_action(action: ActionPlan) -> str:
    label = f"{action.task_id}:{action.kind}"
    if action.tool_name:
        label += f"({action.tool_name})"
    return label


def _is_finish_only(actions: list[ActionPlan]) -> bool:
    return len(actions) == 1 and actions[0].kind == "finish"


def _to_task_result(
    *,
    action: ActionPlan,
    execution: ExecutionResult,
    source: str,
    task_key: str,
    elapsed: float,
) -> TaskExecutionResult:
    return TaskExecutionResult(
        task_id=action.task_id,
        kind=action.kind,
        tool_name=action.tool_name,
        success=execution.success,
        summary=execution.summary,
        findings=list(execution.findings),
        flag_candidates=list(execution.flag_candidates),
        artifacts=_annotate_artifacts(
            execution.artifacts,
            producer_phase=source,
            producer_task_key=task_key,
            producer_action_id=action.task_id,
            producer_tool_name=action.tool_name or action.kind,
            producer_success=execution.success,
        ),
        stdout=execution.stdout,
        stderr=execution.stderr,
        exit_code=execution.exit_code,
        command=execution.command,
        script_path=execution.script_path,
        source=f"{source}:{elapsed:.2f}s",
        failure_stage=execution.failure_stage,
    )


def _aggregate_strategy_execution(results: list[StrategyTaskResult]) -> ExecutionResult | None:
    task_results: list[TaskExecutionResult] = []
    for result in results:
        latest = result.latest_execution
        if latest is None:
            continue
        summary = latest.summary or result.task_summary or result.phase_summary
        task_results.append(
            TaskExecutionResult(
                task_id=result.task_key,
                kind="tool",
                tool_name=result.task_title,
                success=latest.success,
                summary=summary,
                findings=list(latest.findings),
                flag_candidates=list(result.flag_candidates),
                artifacts=list(result.artifacts),
                stdout=latest.stdout,
                stderr=latest.stderr,
                command=latest.command,
                source="strategy_batch",
                failure_stage=latest.failure_stage,
            )
        )
    if not task_results:
        return None
    return _build_batch_execution("strategy_batch", task_results)


def _build_batch_execution(source: str, task_results: list[TaskExecutionResult]) -> ExecutionResult:
    success_count = sum(1 for item in task_results if item.success)
    if not task_results:
        batch_status = "full_fail"
        success = False
    elif success_count == len(task_results):
        batch_status = "full_success"
        success = True
    elif success_count == 0:
        batch_status = "full_fail"
        success = False
    else:
        batch_status = "partial_success"
        success = True

    summaries = [f"[{item.task_id}] {item.summary}" for item in task_results if item.summary]
    findings: list[str] = []
    flags: list[str] = []
    artifacts: list[ArtifactRef] = []
    stdout_parts = []
    stderr_parts = []
    commands = []
    for item in task_results:
        if item.stdout:
            stdout_parts.append(f"[{item.task_id}]\n{item.stdout}")
        if item.stderr:
            stderr_parts.append(f"[{item.task_id}]\n{item.stderr}")
        if item.command:
            commands.append(f"[{item.task_id}] {item.command}")
        for finding in item.findings:
            if finding not in findings:
                findings.append(finding)
        for flag in item.flag_candidates:
            if flag not in flags:
                flags.append(flag)
        artifacts = _merge_artifact_refs(artifacts, item.artifacts)

    failure_stage = ""
    if batch_status == "full_fail":
        failure_stage = next((item.failure_stage for item in task_results if item.failure_stage), "action_execution")

    return ExecutionResult(
        success=success,
        batch_status=batch_status,
        summary="\n".join(summaries),
        findings=findings,
        flag_candidates=flags,
        artifacts=artifacts,
        stdout="\n\n".join(stdout_parts),
        stderr="\n\n".join(stderr_parts),
        exit_code=0 if success else 1,
        command="\n".join(commands),
        source=source,
        failure_stage=failure_stage,
        task_results={item.task_id: item for item in task_results},
    )


def _annotate_artifacts(
    artifacts: list[ArtifactRef],
    *,
    producer_phase: str,
    producer_task_key: str,
    producer_action_id: str,
    producer_tool_name: str,
    producer_success: bool,
) -> list[ArtifactRef]:
    annotated: list[ArtifactRef] = []
    for artifact in artifacts:
        annotated.append(
            artifact.model_copy(
                update={
                    "producer_phase": producer_phase,
                    "producer_task_key": producer_task_key,
                    "producer_action_id": producer_action_id,
                    "producer_tool_name": producer_tool_name,
                    "producer_success": producer_success,
                }
            )
        )
    return _merge_artifact_refs([], annotated)


def _merge_discoveries(left: list[TaskDiscovery], right: list[TaskDiscovery], *, limit: int = 80) -> list[TaskDiscovery]:
    merged = list(left)
    for item in right:
        if not item.summary and not item.findings and not item.flag_candidates and not item.credentials:
            continue
        merged.append(item)
    return merged[-limit:]


def _merge_flag_candidates(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in merged:
                merged.append(item)
    return merged


def _merge_artifact_refs(left: list[ArtifactRef], right: list[ArtifactRef]) -> list[ArtifactRef]:
    merged: list[ArtifactRef] = []
    seen: set[tuple[str, str]] = set()
    for artifact in [*left, *right]:
        key = (artifact.kind, artifact.path)
        if key in seen:
            continue
        seen.add(key)
        merged.append(artifact)
    return merged


def _agent_trace(agent: object) -> dict | None:
    trace = getattr(agent, "last_trace", None)
    return dict(trace) if isinstance(trace, dict) else None


def _execution_message(execution: ExecutionResult) -> str:
    for item in execution.task_results.values():
        if item.summary:
            return f"[{item.task_id}] {item.summary}"
        if item.stderr.strip():
            return f"[{item.task_id}] {item.stderr.strip().splitlines()[0]}"
        if item.stdout.strip():
            return f"[{item.task_id}] {item.stdout.strip().splitlines()[0]}"
    return execution.summary or "Action execution failed"


def _style(text: str, color: str = "", *, bold: bool = False) -> str:
    if not sys.stdout.isatty():
        return text
    prefix = ""
    if bold:
        prefix += ANSI_BOLD
    if color:
        prefix += color
    return f"{prefix}{text}{ANSI_RESET}" if prefix else text


def _excerpt(value: str, limit: int = 600) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _excerpt_head_tail(value: str, limit: int = 6000) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    if limit <= 32:
        return _excerpt(rendered, limit=limit)
    marker = "\n...\n[truncated]\n...\n"
    available = limit - len(marker)
    if available <= 32:
        return _excerpt(rendered, limit=limit)
    head = available // 2
    tail = available - head
    return rendered[:head] + marker + rendered[-tail:]
