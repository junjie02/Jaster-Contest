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
    CodeEvidence,
    CompressionNote,
    ContestControlAction,
    ContestControlResult,
    ExecutionResult,
    FailurePattern,
    InjectedSkill,
    LatestExecutionResult,
    Observation,
    ObservedTaskResult,
    PlanInput,
    PlannerContext,
    PlannerHistoryEntry,
    PlanningAttempt,
    PersistentCodeEvidence,
    RecentObservationRound,
    ReflectionHistoryEntry,
    ReflectionInput,
    ReflectionOutput,
    ReflectionTaskUpdate,
    RoutableTask,
    RunState,
    SkillCard,
    SharedBulletinDigest,
    SharedBulletinEntry,
    SharedFinding,
    StrategicRejection,
    StrategyInput,
    StrategyTaskResult,
    SubmissionAttempt,
    TaskDiscovery,
    TaskDependencyContext,
    TaskSkillBinding,
    TaskSkillSelection,
    TaskExecutionResult,
    TaskNodeSnapshot,
    TaskNodeUpdatePatch,
    TaskStatus,
    TaskStatusDigest,
    TaskStatusDigestItem,
    TeamManagerInput,
    TeamManagerOutput,
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
        verified_window: int = 4,
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
        return SharedBulletinDigest(
            new_entries=new_entries,
            verified_entries=verified_entries,
            unverified_entries=[],
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
        self.llm = llm
        self.agents = build_agents(prompt_root, llm)
        self.verbose = verbose
        self.phase_max_retries = env_int("JASTER_PHASE_MAX_RETRIES", 3)
        self.parallel_task_workers = env_int("JASTER_PARALLEL_TASK_WORKERS", 4)
        self.parallel_action_workers = env_int("JASTER_PARALLEL_ACTION_WORKERS", 4)
        self.strategy_max_rounds = env_int("JASTER_STRATEGY_MAX_ROUNDS", 8)
        self.strategy_observation_limit = env_int("JASTER_STRATEGY_RECENT_OBSERVATION_LIMIT", 8)
        self.default_tool_timeout = env_int("JASTER_MCP_TOOL_TIMEOUT", 180)
        self.planner_context_window = env_int("JASTER_PLANNER_CONTEXT_WINDOW", 8)
        self.context_payload_limit = env_int("JASTER_CONTEXT_PAYLOAD_LIMIT", 150000)
        self.skill_catalog = _load_skill_catalog(skills_dir)
        self._skill_catalog_by_name = {item.name.lower(): item for item in self.skill_catalog}
        self._skill_body_cache: dict[str, str] = {}
        self._on_tree_update = on_tree_update

    def run(
        self,
        challenge: ChallengeSpec,
        *,
        max_rounds: int = 12,
        reflection_control_handler: callable | None = None,
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
            plan_input = self._build_plan_input(
                challenge=challenge,
                task_tree=state.task_tree,
                bootstrap_execution=bootstrap_execution,
                planner_context=state.planner_context,
                reflection_history=state.reflection_history,
                latest_discoveries=state.latest_discoveries,
                available_artifacts=state.available_artifacts,
                submitted_flags=state.submitted_flags,
                incorrect_flags=state.incorrect_flags,
                submission_history=state.submission_history,
            )
            plan_out, plan_elapsed = self._timed_agent_run(
                "plan",
                challenge.zone,
                plan_input,
            )
            self._log(f"    LLM time: {plan_elapsed:.2f}s")
            task_tree.apply_patch(plan_out.tree_patch)
            state.task_tree = task_tree.snapshot()
            added_keys = [node.key for node in state.task_tree.nodes if node.key not in previous_keys]

            dispatch_keys = self._resolve_dispatch_keys(task_tree, plan_out.dispatch_task_keys)
            dispatch_keys = self._merge_auto_dispatch_keys(task_tree, dispatch_keys, added_keys)
            self._log_plan_cycle(cycle, task_tree, plan_out, dispatch_keys)
            control_results: list[ContestControlResult] = []
            if getattr(plan_out, "control_actions", None) and reflection_control_handler:
                control_results = reflection_control_handler(
                    challenge,
                    list(plan_out.control_actions),
                    state,
                    cycle,
                ) or []
                self._apply_control_results(state, challenge, cycle, control_results)
                self._log_reflection_control(cycle, control_results)
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
                    "input_payload": plan_input.model_dump(),
                    "output": plan_out.model_dump(),
                    "control_results": [item.model_dump() for item in control_results],
                },
            )
            self.store.save_state(state)
            self._notify_tree_update(state.task_tree)

            if challenge.flag_count > 0 and challenge.flag_got_count >= challenge.flag_count:
                self._log("[*] Run stopping: all flags submitted during planning")
                state.rounds_completed = cycle
                self.store.save_state(state)
                self._notify_tree_update(state.task_tree)
                break

            if not dispatch_keys:
                in_progress = [node.key for node in state.task_tree.nodes if node.status == TaskStatus.in_progress]
                if not in_progress:
                    self._log("[*] Run stopping: planner dispatched no tasks and no in-progress tasks remain")
                    state.rounds_completed = cycle - 1
                    self.store.save_state(state)
                    self._notify_tree_update(state.task_tree)
                    break
                dispatch_keys = in_progress

            state.skill_bindings = self._prune_skill_bindings(state.task_tree, state.skill_bindings)
            state.skill_bindings = self._ensure_task_skill_bindings(
                run_id=run_id,
                cycle=cycle,
                challenge=challenge,
                task_tree=state.task_tree,
                task_keys=dispatch_keys,
                skill_bindings=state.skill_bindings,
            )
            self.store.save_state(state)
            self._log_effective_skill_bindings(
                task_tree=state.task_tree,
                task_keys=dispatch_keys,
                skill_bindings=state.skill_bindings,
            )

            self._log(f"[*] Cycle {cycle}: strategy batch | tasks={len(dispatch_keys)}")
            strategy_results, observations, batch_discoveries, batch_execution, bulletin_board = self._run_strategy_batch(
                run_id=run_id,
                cycle=cycle,
                challenge=challenge,
                task_tree=state.task_tree,
                task_keys=dispatch_keys,
                reflection_history=state.reflection_history,
                available_artifacts=state.available_artifacts,
                persistent_code_evidence=state.persistent_code_evidence,
                skill_bindings=state.skill_bindings,
                observations=state.observations,
                shared_bulletin=state.shared_bulletin,
                submitted_flags=state.submitted_flags,
                incorrect_flags=state.incorrect_flags,
                submission_history=state.submission_history,
            )

            state.observations.extend(observations)
            state.latest_discoveries = _merge_discoveries(state.latest_discoveries, batch_discoveries)
            state.persistent_code_evidence = _merge_persistent_code_evidence(
                state.persistent_code_evidence,
                [item for result in strategy_results for item in result.code_evidence],
            )
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
                    challenge_context=_challenge_context(
                        challenge,
                        submitted_flags=state.submitted_flags,
                    incorrect_flags=state.incorrect_flags,
                    submission_history=state.submission_history,
                ),
                    strategy_results=strategy_results,
                    candidate_flags=_merge_flag_candidates(
                        *[result.flag_candidates for result in strategy_results],
                    ),
                    submitted_flags=list(state.submitted_flags),
                    incorrect_flags=list(state.incorrect_flags),
                    submission_history=list(state.submission_history[-6:]),
                    reflection_history=list(state.reflection_history),
                    latest_discoveries=_prompt_discoveries(batch_discoveries),
                    available_artifacts=_prompt_artifacts(state.available_artifacts),
                    available_control_tools=[],
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
                control_results=[],
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
                    "control_results": [],
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
        persistent_code_evidence: list[PersistentCodeEvidence],
        skill_bindings: list[TaskSkillBinding],
        observations: list[Observation],
        shared_bulletin: list[SharedBulletinEntry],
        submitted_flags: list[str],
        incorrect_flags: list[str],
        submission_history: list[SubmissionAttempt],
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
                    persistent_code_evidence=persistent_code_evidence,
                    skill_bindings=skill_bindings,
                    observations=observations,
                    bulletin_board=bulletin_board,
                    submitted_flags=submitted_flags,
                    incorrect_flags=incorrect_flags,
                    submission_history=submission_history,
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
        persistent_code_evidence: list[PersistentCodeEvidence],
        skill_bindings: list[TaskSkillBinding],
        observations: list[Observation],
        bulletin_board: _StrategyBulletinBoard,
        submitted_flags: list[str],
        incorrect_flags: list[str],
        submission_history: list[SubmissionAttempt],
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
        new_code_evidence: list[PersistentCodeEvidence] = []
        assigned_skill = self._resolve_injected_skill(task_node, skill_bindings)
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
            strategy_input = self._build_strategy_input(
                challenge=challenge,
                task_tree=task_tree,
                task_node=task_node,
                assigned_skill=assigned_skill,
                recent_rounds=recent_rounds,
                latest_execution=latest_execution,
                reflection_history=reflection_history,
                available_artifacts=available_artifacts,
                persistent_code_evidence=_persistent_code_evidence_for_task(
                    _merge_persistent_code_evidence(persistent_code_evidence, new_code_evidence),
                    task_tree,
                    task_node.key,
                    limit=6,
                ),
                bulletin_digest=bulletin_digest,
                submitted_flags=submitted_flags,
                incorrect_flags=incorrect_flags,
                submission_history=submission_history,
            )

            try:
                strategy_out, _ = self._timed_agent_run(
                    "strategy",
                    challenge.zone,
                    strategy_input,
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
            new_code_evidence = _merge_persistent_code_evidence(
                new_code_evidence,
                _persist_code_evidence_entries(
                    cycle=cycle,
                    task_node=task_node,
                    evidence=list(strategy_out.code_evidence),
                ),
            )
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
            code_evidence=list(new_code_evidence),
        )
        self._log_task_result(result)
        return result, collected

    def _build_plan_input(
        self,
        *,
        challenge: ChallengeSpec,
        task_tree: TaskTreeSnapshot,
        bootstrap_execution: ExecutionResult | None,
        planner_context: PlannerContext | None,
        reflection_history: list[ReflectionHistoryEntry],
        latest_discoveries: list[TaskDiscovery],
        available_artifacts: list[ArtifactRef],
        submitted_flags: list[str],
        incorrect_flags: list[str],
        submission_history: list[SubmissionAttempt],
    ) -> PlanInput:
        payload = PlanInput(
            objective=f"Plan the next batch of penetration tasks for {challenge.target}.",
            task_tree=_prompt_task_tree(task_tree),
            challenge_context=_challenge_context(
                challenge,
                submitted_flags=submitted_flags,
                incorrect_flags=incorrect_flags,
                submission_history=submission_history,
            ),
            bootstrap_execution=_compact_execution(bootstrap_execution),
            planner_context=planner_context.model_copy(deep=True) if planner_context else None,
            task_status_summary=_task_status_summary(task_tree),
            failure_patterns_summary=_failure_patterns_summary(reflection_history),
            task_status_digest=_task_status_digest(task_tree),
            failure_patterns_digest=_failure_patterns_digest(reflection_history),
            candidate_flags=_merge_flag_candidates(*[item.flag_candidates for item in latest_discoveries[-12:]]),
            submitted_flags=list(submitted_flags),
            incorrect_flags=list(incorrect_flags),
            submission_history=list(submission_history[-6:]),
            reflection_history=[item.model_copy(deep=True) for item in reflection_history],
            latest_discoveries=_prompt_discoveries(latest_discoveries, limit=20),
            available_artifacts=_prompt_artifacts(available_artifacts, limit=15),
            available_control_tools=_contest_control_tools(),
        )
        return self._compress_plan_input(payload)

    def _build_strategy_input(
        self,
        *,
        challenge: ChallengeSpec,
        task_tree: TaskTreeSnapshot,
        task_node: TaskNodeSnapshot,
        assigned_skill: InjectedSkill | None = None,
        recent_rounds: list[RecentObservationRound],
        latest_execution: ExecutionResult | None,
        reflection_history: list[ReflectionHistoryEntry],
        available_artifacts: list[ArtifactRef],
        persistent_code_evidence: list[PersistentCodeEvidence],
        bulletin_digest: SharedBulletinDigest,
        submitted_flags: list[str] | None = None,
        incorrect_flags: list[str] | None = None,
        submission_history: list[SubmissionAttempt] | None = None,
    ) -> StrategyInput:
        focus_tree = _task_tree_focus(task_tree, task_node.key, limit=12)
        dependency_context = _dependency_context(task_tree, task_node.key, available_artifacts, limit=6)
        payload = StrategyInput(
            objective=f"Complete assigned task: {task_node.title}",
            assigned_task=task_node,
            task_tree=TaskTreeSnapshot(nodes=[]),
            task_tree_focus=focus_tree,
            challenge_context=_challenge_context(
                challenge,
                submitted_flags=submitted_flags or [],
                incorrect_flags=incorrect_flags or [],
                submission_history=submission_history or [],
            ),
            assigned_skill=assigned_skill.model_copy(deep=True) if assigned_skill else None,
            recent_observations=list(recent_rounds),
            latest_execution=_compact_execution(latest_execution),
            reflection_history=[item.model_copy(deep=True) for item in reflection_history],
            dependency_context=dependency_context,
            persistent_code_evidence=[item.model_copy(deep=True) for item in persistent_code_evidence],
            available_artifacts=_prompt_artifacts(available_artifacts, limit=10),
            available_tools=_available_tools_compact(),
            shared_bulletin=SharedBulletinDigest(
                new_entries=[item.model_copy(deep=True) for item in bulletin_digest.new_entries],
                verified_entries=[item.model_copy(deep=True) for item in bulletin_digest.verified_entries[-4:]],
                unverified_entries=[],
            ),
        )
        return self._compress_strategy_input(payload)

    def _compress_plan_input(self, payload: PlanInput) -> PlanInput:
        if _payload_chars(payload) <= self.context_payload_limit:
            return payload

        notes: list[CompressionNote] = list(payload.compression_notes)
        payload = payload.model_copy(deep=True)

        payload.planner_context = self._compress_planner_context(payload.planner_context, notes)
        payload.reflection_digest = self._summarize_reflection_history(payload.reflection_history[:-4], "plan reflection history", notes)
        payload.reflection_history = payload.reflection_history[-4:]
        payload.latest_discoveries, payload.discoveries_digest = self._compress_discoveries(
            payload.latest_discoveries,
            "plan latest discoveries",
            keep=12,
            notes=notes,
        )
        payload.available_artifacts = _prompt_artifacts(payload.available_artifacts, limit=12)
        payload.compression_notes = notes
        if _payload_chars(payload) > self.context_payload_limit:
            payload = self._shrink_plan_windows(payload, notes)
        if _payload_chars(payload) > self.context_payload_limit:
            raise RuntimeError("protected_context_overflow: plan payload exceeds limit after compressing non-protected fields")
        return payload

    def _compress_strategy_input(self, payload: StrategyInput) -> StrategyInput:
        notes: list[CompressionNote] = list(payload.compression_notes)
        payload = payload.model_copy(deep=True)

        payload.reflection_digest = self._summarize_reflection_history(
            payload.reflection_history[:-1],
            "strategy reflection history",
            notes,
        )
        payload.reflection_history = payload.reflection_history[-1:]

        if _payload_chars(payload) <= self.context_payload_limit:
            payload.compression_notes = notes
            return payload

        payload.recent_observations, payload.observation_digest = self._compress_observations(
            payload.recent_observations,
            keep=3,
            field_name="strategy recent observations",
            notes=notes,
        )
        payload.shared_bulletin, payload.bulletin_digest = self._compress_bulletin(
            payload.shared_bulletin,
            notes=notes,
        )
        payload.available_artifacts = _prompt_artifacts(payload.available_artifacts, limit=8)
        payload.compression_notes = notes
        if _payload_chars(payload) > self.context_payload_limit:
            payload = self._shrink_strategy_windows(payload, notes)
        if _payload_chars(payload) > self.context_payload_limit:
            raise RuntimeError("protected_context_overflow: strategy payload exceeds limit after compressing non-protected fields")
        return payload

    def _compress_planner_context(
        self,
        context: PlannerContext | None,
        notes: list[CompressionNote],
    ) -> PlannerContext | None:
        if context is None:
            return None
        context = context.model_copy(deep=True)
        if len(context.planning_attempts) <= 6:
            return context
        older = context.planning_attempts[:-6]
        source_payload = {
            "existing_compressed_history_summary": context.compressed_history_summary,
            "older_planning_attempts": [item.model_dump() for item in older],
        }
        source = json.dumps(source_payload, ensure_ascii=False, indent=2)
        digest = self._summarize_text(title="planner context planning attempts", text=source)
        if digest:
            notes.append(
                CompressionNote(
                    field="planner_context.planning_attempts",
                    reason="compressed older planning attempts with llm",
                    original_chars=len(source),
                    final_chars=len(digest),
                    strategy="llm_summary",
                )
            )
            context.compressed_history_summary = digest
            context.compression_count += 1
        context.planning_attempts = context.planning_attempts[-6:]
        return context

    def _compress_observations(
        self,
        rounds: list[RecentObservationRound],
        *,
        keep: int,
        field_name: str,
        notes: list[CompressionNote],
    ) -> tuple[list[RecentObservationRound], str]:
        if len(rounds) <= keep:
            return rounds, ""
        older = rounds[:-keep]
        digest = self._summarize_text(
            title=field_name,
            text=json.dumps([item.model_dump() for item in older], ensure_ascii=False, indent=2),
        )
        if digest:
            notes.append(
                CompressionNote(
                    field=field_name,
                    reason="compressed older rounds with llm",
                    original_chars=len(json.dumps([item.model_dump() for item in older], ensure_ascii=False)),
                    final_chars=len(digest),
                    strategy="llm_summary",
                )
            )
        return rounds[-keep:], digest

    def _summarize_reflection_history(
        self,
        history: list[ReflectionHistoryEntry],
        field_name: str,
        notes: list[CompressionNote],
    ) -> str:
        if not history:
            return ""
        source = json.dumps([item.model_dump() for item in history], ensure_ascii=False, indent=2)
        digest = self._summarize_text(title=field_name, text=source)
        if digest:
            notes.append(
                CompressionNote(
                    field=field_name,
                    reason="compressed older reflection history with llm",
                    original_chars=len(source),
                    final_chars=len(digest),
                    strategy="llm_summary",
                )
            )
        return digest

    def _compress_discoveries(
        self,
        discoveries: list[TaskDiscovery],
        field_name: str,
        *,
        keep: int,
        notes: list[CompressionNote],
    ) -> tuple[list[TaskDiscovery], str]:
        if len(discoveries) <= keep:
            return discoveries, ""
        older = discoveries[:-keep]
        source = json.dumps([item.model_dump() for item in older], ensure_ascii=False, indent=2)
        digest = self._summarize_text(title=field_name, text=source)
        if digest:
            notes.append(
                CompressionNote(
                    field=field_name,
                    reason="compressed older discoveries with llm",
                    original_chars=len(source),
                    final_chars=len(digest),
                    strategy="llm_summary",
                )
            )
        return discoveries[-keep:], digest

    def _compress_bulletin(
        self,
        bulletin: SharedBulletinDigest,
        *,
        notes: list[CompressionNote],
    ) -> tuple[SharedBulletinDigest, str]:
        if len(bulletin.new_entries) <= 8:
            return bulletin, ""
        older = bulletin.new_entries[:-8]
        source = json.dumps([item.model_dump() for item in older], ensure_ascii=False, indent=2)
        digest = self._summarize_text(title="strategy older unread bulletin entries", text=source)
        if digest:
            notes.append(
                CompressionNote(
                    field="shared_bulletin.new_entries",
                    reason="compressed older unread bulletin entries with llm",
                    original_chars=len(source),
                    final_chars=len(digest),
                    strategy="llm_summary",
                )
            )
        return SharedBulletinDigest(
            new_entries=[item.model_copy(deep=True) for item in bulletin.new_entries[-8:]],
            verified_entries=[item.model_copy(deep=True) for item in bulletin.verified_entries],
            unverified_entries=[],
        ), digest

    def _shrink_plan_windows(self, payload: PlanInput, notes: list[CompressionNote]) -> PlanInput:
        payload = payload.model_copy(deep=True)

        for keep in (4, 2, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.planner_context = self._trim_planner_attempt_window(payload.planner_context, keep=keep, notes=notes)

        for keep in (2, 1, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.reflection_history = self._trim_list_window(
                payload.reflection_history,
                keep=keep,
                field="plan reflection history window",
                notes=notes,
            )

        for keep in (8, 4, 2, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.latest_discoveries = self._trim_list_window(
                payload.latest_discoveries,
                keep=keep,
                field="plan latest discoveries window",
                notes=notes,
            )

        for limit in (8, 4, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            trimmed = _prompt_artifacts(payload.available_artifacts, limit=limit)
            self._append_window_reduction_note(
                field="plan available_artifacts",
                before=payload.available_artifacts,
                after=trimmed,
                notes=notes,
            )
            payload.available_artifacts = trimmed

        payload.compression_notes = list(notes)
        if _payload_chars(payload) > self.context_payload_limit:
            for keep in (8, 4, 0):
                if _payload_chars(payload) <= self.context_payload_limit:
                    break
                payload.compression_notes = payload.compression_notes[-keep:] if keep > 0 else []
        return payload

    def _shrink_strategy_windows(self, payload: StrategyInput, notes: list[CompressionNote]) -> StrategyInput:
        payload = payload.model_copy(deep=True)

        for keep in (2, 1, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.recent_observations = self._trim_list_window(
                payload.recent_observations,
                keep=keep,
                field="strategy recent observations window",
                notes=notes,
            )

        for keep in (2, 1, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.reflection_history = self._trim_list_window(
                payload.reflection_history,
                keep=keep,
                field="strategy reflection history window",
                notes=notes,
            )

        for keep in (4, 2, 1, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            trimmed = self._trim_bulletin_window(
                payload.shared_bulletin,
                new_keep=max(keep * 2, 0),
                verified_keep=keep,
            )
            self._append_window_reduction_note(
                field="shared_bulletin windows",
                before=payload.shared_bulletin,
                after=trimmed,
                notes=notes,
            )
            payload.shared_bulletin = trimmed

        for keep in (4, 2, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.dependency_context = self._trim_list_window(
                payload.dependency_context,
                keep=keep,
                field="strategy dependency_context",
                notes=notes,
            )

        for keep in (4, 2, 1, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.persistent_code_evidence = self._trim_list_window(
                payload.persistent_code_evidence,
                keep=keep,
                field="strategy persistent_code_evidence",
                notes=notes,
            )

        for keep in (8, 6, 4, 2):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            payload.task_tree_focus = self._trim_task_tree_window(
                payload.task_tree_focus,
                keep=keep,
                field="strategy task_tree_focus",
                notes=notes,
            )

        for limit in (6, 3, 0):
            if _payload_chars(payload) <= self.context_payload_limit:
                break
            trimmed = _prompt_artifacts(payload.available_artifacts, limit=limit)
            self._append_window_reduction_note(
                field="strategy available_artifacts",
                before=payload.available_artifacts,
                after=trimmed,
                notes=notes,
            )
            payload.available_artifacts = trimmed

        payload.compression_notes = list(notes)
        if _payload_chars(payload) > self.context_payload_limit:
            for keep in (8, 4, 0):
                if _payload_chars(payload) <= self.context_payload_limit:
                    break
                payload.compression_notes = payload.compression_notes[-keep:] if keep > 0 else []
        return payload

    def _trim_planner_attempt_window(
        self,
        context: PlannerContext | None,
        *,
        keep: int,
        notes: list[CompressionNote],
    ) -> PlannerContext | None:
        if context is None:
            return None
        context = context.model_copy(deep=True)
        if len(context.planning_attempts) <= keep:
            return context
        before = context.model_dump()
        context.planning_attempts = context.planning_attempts[-keep:] if keep > 0 else []
        self._append_window_reduction_note(
            field="planner_context active window",
            before=before,
            after=context.model_dump(),
            notes=notes,
        )
        return context

    def _trim_list_window(
        self,
        items: list[Any],
        *,
        keep: int,
        field: str,
        notes: list[CompressionNote],
    ) -> list[Any]:
        if len(items) <= keep:
            return items
        trimmed = items[-keep:] if keep > 0 else []
        self._append_window_reduction_note(field=field, before=items, after=trimmed, notes=notes)
        return trimmed

    def _trim_task_tree_window(
        self,
        snapshot: TaskTreeSnapshot,
        *,
        keep: int,
        field: str,
        notes: list[CompressionNote],
    ) -> TaskTreeSnapshot:
        if len(snapshot.nodes) <= keep:
            return snapshot
        trimmed = TaskTreeSnapshot(nodes=[item.model_copy(deep=True) for item in snapshot.nodes[:keep]])
        self._append_window_reduction_note(field=field, before=snapshot, after=trimmed, notes=notes)
        return trimmed

    def _trim_bulletin_window(
        self,
        bulletin: SharedBulletinDigest,
        *,
        new_keep: int,
        verified_keep: int,
    ) -> SharedBulletinDigest:
        return SharedBulletinDigest(
            new_entries=[item.model_copy(deep=True) for item in bulletin.new_entries[-new_keep:]],
            verified_entries=[item.model_copy(deep=True) for item in bulletin.verified_entries[-verified_keep:]],
            unverified_entries=[],
        )

    def _append_window_reduction_note(
        self,
        *,
        field: str,
        before: Any,
        after: Any,
        notes: list[CompressionNote],
    ) -> None:
        before_chars = len(json.dumps(_jsonable(before), ensure_ascii=False))
        after_chars = len(json.dumps(_jsonable(after), ensure_ascii=False))
        if after_chars >= before_chars:
            return
        notes.append(
            CompressionNote(
                field=field,
                reason="reduced retained raw window after summarizing older context",
                original_chars=before_chars,
                final_chars=after_chars,
                strategy="window_reduce",
            )
        )

    def _summarize_text(self, *, title: str, text: str) -> str:
        if not text.strip():
            return ""
        if not hasattr(self.llm, "complete_text"):
            raise RuntimeError(f"llm_summary_unavailable: cannot compress {title}")
        prompt = (
            "请将以下上下文压缩为简洁但保真的中文摘要，保留任务目标、关键发现、失败模式、可复用路径、"
            "凭据、payload、文件路径和下一步决策相关信息。不要杜撰，不要省略明确证据。\n\n"
            f"上下文类型: {title}\n\n{text}"
        )
        system = "你是一个渗透测试上下文压缩器。你的职责是在不改变事实的前提下压缩较老的历史上下文。"
        return str(self.llm.complete_text(system=system, prompt=prompt) or "").strip()

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

    def _apply_control_results(
        self,
        state: RunState,
        challenge: ChallengeSpec,
        cycle: int,
        control_results: list[ContestControlResult],
    ) -> None:
        if not control_results:
            return

        state.latest_submission_results = [item.model_copy(deep=True) for item in control_results]
        for result in control_results:
            if result.kind == "submit_flag":
                if result.attempted and result.flag:
                    state.submission_history.append(
                        SubmissionAttempt(
                            cycle=cycle,
                            flag=result.flag,
                            reason=result.reason,
                            correct=bool(result.correct),
                            message=result.message,
                            progress_before=result.progress_before,
                            progress_after=result.progress_after,
                        )
                    )
                if result.correct and result.flag and result.flag not in state.submitted_flags:
                    state.submitted_flags.append(result.flag)
                if result.correct is False and result.flag and result.flag not in state.incorrect_flags:
                    state.incorrect_flags.append(result.flag)
                if result.flag_count:
                    challenge.flag_count = result.flag_count
                    state.challenge.flag_count = result.flag_count
                if result.flag_got_count:
                    challenge.flag_got_count = result.flag_got_count
                    state.challenge.flag_got_count = result.flag_got_count
            elif result.kind == "view_hint" and result.hint_content:
                challenge.hint_content = result.hint_content
                state.challenge.hint_content = result.hint_content

        discovery_items: list[TaskDiscovery] = []
        for result in control_results:
            if result.kind == "submit_flag" and result.attempted:
                summary = (
                    f"平台提交 Flag {result.flag} {'成功' if result.correct else '失败'}: {result.message}"
                    if result.flag
                    else f"平台提交结果: {result.message}"
                )
                discovery_items.append(
                    TaskDiscovery(
                        cycle=cycle,
                        task_key="__platform__",
                        task_title="Contest Control",
                        source="reflection_control",
                        summary=summary,
                        findings=[
                            item
                            for item in [
                                f"progress: {result.progress_before} -> {result.progress_after}" if result.progress_before or result.progress_after else "",
                                result.message,
                            ]
                            if item
                        ],
                        flag_candidates=[result.flag] if result.correct and result.flag else [],
                    )
                )
            if result.kind == "view_hint" and result.attempted and result.hint_content:
                discovery_items.append(
                    TaskDiscovery(
                        cycle=cycle,
                        task_key="__platform__",
                        task_title="Contest Control",
                        source="reflection_control",
                        summary="平台提示已获取",
                        findings=[result.hint_content],
                    )
                )
        if discovery_items:
            state.latest_discoveries = _merge_discoveries(state.latest_discoveries, discovery_items)

    def _log_reflection_control(self, cycle: int, control_results: list[ContestControlResult]) -> None:
        for result in control_results:
            if result.kind == "submit_flag":
                if result.attempted and result.correct:
                    label = "[control:submit:ok]"
                    tone = ANSI_GREEN
                elif result.attempted:
                    label = "[control:submit:fail]"
                    tone = ANSI_RED
                else:
                    label = "[control:submit:skip]"
                    tone = ANSI_YELLOW
                progress = ""
                if result.progress_before or result.progress_after:
                    progress = f" | progress={result.progress_before or '-'} -> {result.progress_after or '-'}"
                detail = _excerpt(result.message or result.reason, limit=180)
                self._log(
                    "    "
                    + _style(label, tone, bold=True)
                    + f" cycle={cycle} | flag={_excerpt(result.flag, limit=80)}{progress} | {detail}"
                )
                continue

            if result.kind == "view_hint":
                label = "[control:hint:ok]" if result.attempted else "[control:hint:skip]"
                tone = ANSI_MAGENTA if result.attempted else ANSI_YELLOW
                detail = _excerpt(result.hint_content or result.message or result.reason, limit=200)
                self._log(
                    "    "
                    + _style(label, tone, bold=True)
                    + f" cycle={cycle} | {detail}"
                )

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

    def _prune_skill_bindings(
        self,
        task_tree: TaskTreeSnapshot,
        skill_bindings: list[TaskSkillBinding],
    ) -> list[TaskSkillBinding]:
        nodes_by_key = {node.key: node for node in task_tree.nodes}
        deduped: dict[str, TaskSkillBinding] = {}
        for binding in skill_bindings:
            if binding.task_key not in nodes_by_key:
                continue
            deduped[binding.task_key] = binding.model_copy(deep=True)
        return list(deduped.values())

    def _ensure_task_skill_bindings(
        self,
        *,
        run_id: str,
        cycle: int,
        challenge: ChallengeSpec,
        task_tree: TaskTreeSnapshot,
        task_keys: list[str],
        skill_bindings: list[TaskSkillBinding],
    ) -> list[TaskSkillBinding]:
        binding_map = {item.task_key: item.model_copy(deep=True) for item in skill_bindings}
        nodes_by_key = {node.key: node for node in task_tree.nodes}
        tasks_to_route = [
            nodes_by_key[key]
            for key in task_keys
            if key in nodes_by_key and not _binding_matches_task(binding_map.get(key), nodes_by_key[key])
        ]
        if not tasks_to_route or "team_manager" not in self.agents or not self.skill_catalog:
            return list(binding_map.values())

        payload = self._build_team_manager_input(challenge=challenge, tasks=tasks_to_route)
        team_out, elapsed = self._timed_agent_run("team_manager", challenge.zone, payload)
        self._log(f"[*] Cycle {cycle}: team manager")
        self._log(f"    LLM time: {elapsed:.2f}s")
        self._log_team_manager_cycle(tasks_to_route, team_out)

        assignments_by_key = {
            item.task_key: item.model_copy(deep=True)
            for item in team_out.assignments
            if item.task_key
        }
        for task_node in tasks_to_route:
            selection = assignments_by_key.get(task_node.key)
            binding_map[task_node.key] = self._selection_to_binding(task_node, selection)

        self.store.append_round(
            run_id,
            f"team_manager_round_{cycle:03d}",
            {
                "cycle": cycle,
                "agent": "team_manager",
                "input": _agent_trace(self.agents.get("team_manager")),
                "input_payload": payload.model_dump(),
                "output": team_out.model_dump(),
            },
        )
        return list(binding_map.values())

    def _build_team_manager_input(
        self,
        *,
        challenge: ChallengeSpec,
        tasks: list[TaskNodeSnapshot],
    ) -> TeamManagerInput:
        return TeamManagerInput(
            objective="Assign at most one skill to each dispatched task.",
            challenge_context=_challenge_context(challenge),
            tasks=[
                RoutableTask(
                    task_key=item.key,
                    title=item.title,
                    reason=item.reason,
                    completion_criteria=item.completion_criteria,
                    latest_summary=item.latest_summary,
                    latest_findings=list(item.latest_findings),
                )
                for item in tasks
            ],
            available_skills=[item.model_copy(deep=True) for item in self.skill_catalog],
        )

    def _selection_to_binding(
        self,
        task_node: TaskNodeSnapshot,
        selection: TaskSkillSelection | None,
    ) -> TaskSkillBinding:
        task_signature = _task_signature(task_node)
        if selection is None or selection.no_match or not selection.skill_name.strip():
            return TaskSkillBinding(
                task_key=task_node.key,
                task_signature=task_signature,
                selection_reason="" if selection is None else selection.selection_reason,
                confidence=0.0 if selection is None else selection.confidence,
                no_match=True,
            )

        catalog_item = self._skill_catalog_by_name.get(selection.skill_name.strip().lower())
        if catalog_item is None:
            raise ValueError(f"team manager returned unknown skill: {selection.skill_name}")

        return TaskSkillBinding(
            task_key=task_node.key,
            task_signature=task_signature,
            skill_name=catalog_item.name,
            skill_path=catalog_item.source_path,
            selection_reason=selection.selection_reason,
            confidence=selection.confidence,
            no_match=False,
        )

    def _resolve_injected_skill(
        self,
        task_node: TaskNodeSnapshot,
        skill_bindings: list[TaskSkillBinding],
    ) -> InjectedSkill | None:
        binding = next(
            (
                item
                for item in skill_bindings
                if item.task_key == task_node.key and _binding_matches_task(item, task_node)
            ),
            None,
        )
        if binding is None or binding.no_match or not binding.skill_path:
            return None

        body = self._skill_body_cache.get(binding.skill_path)
        if body is None:
            _, body = _read_skill_markdown(Path(binding.skill_path))
            self._skill_body_cache[binding.skill_path] = body

        catalog_item = self._skill_catalog_by_name.get(binding.skill_name.lower())
        summary = catalog_item.summary if catalog_item else ""
        use_when = catalog_item.use_when if catalog_item else ""
        return InjectedSkill(
            name=binding.skill_name,
            summary=summary,
            use_when=use_when,
            body=body,
            selection_reason=binding.selection_reason,
            confidence=binding.confidence,
            source_path=binding.skill_path,
        )

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
        for action in list(getattr(plan_out, "control_actions", []) or [])[:4]:
            detail = action.flag if action.kind == "submit_flag" else action.reason
            self._log(
                "    "
                + _style("[plan:control]", ANSI_BLUE, bold=True)
                + f" {action.kind} | {_excerpt(detail, limit=180)}"
            )
        for key in dispatch_keys:
            node = task_tree.get(key)
            title = node.title if node else key
            self._log("    " + _style("[plan:dispatch]", ANSI_BLUE, bold=True) + f" {key} | {title}")

    def _log_team_manager_cycle(
        self,
        tasks: list[TaskNodeSnapshot],
        team_out: TeamManagerOutput,
    ) -> None:
        if team_out.phase_summary:
            self._log(
                f"    {_style('[team:summary]', ANSI_GREEN, bold=True)} {_excerpt(team_out.phase_summary, limit=240)}"
            )
        tasks_by_key = {item.key: item for item in tasks}
        for assignment in team_out.assignments[:8]:
            task = tasks_by_key.get(assignment.task_key)
            title = task.title if task else assignment.task_key
            if assignment.no_match or not assignment.skill_name:
                detail = assignment.selection_reason or "no matching skill"
                self._log(
                    "    "
                    + _style("[team:no-match]", ANSI_YELLOW, bold=True)
                    + f" {assignment.task_key} | {title} | {_excerpt(detail, limit=180)}"
                )
                continue
            self._log(
                "    "
                + _style("[team:skill]", ANSI_GREEN, bold=True)
                + f" {assignment.task_key} | {title} -> {assignment.skill_name}"
                + f" | {_excerpt(assignment.selection_reason, limit=160)}"
            )

    def _log_effective_skill_bindings(
        self,
        *,
        task_tree: TaskTreeSnapshot,
        task_keys: list[str],
        skill_bindings: list[TaskSkillBinding],
    ) -> None:
        nodes_by_key = {node.key: node for node in task_tree.nodes}
        bindings_by_key = {item.task_key: item for item in skill_bindings}
        for key in task_keys:
            node = nodes_by_key.get(key)
            if node is None:
                continue
            binding = bindings_by_key.get(key)
            if binding is None or binding.no_match or not binding.skill_name:
                self._log(
                    "    "
                    + _style("[task:skill]", ANSI_MAGENTA, bold=True)
                    + f" {key} | {node.title} | no injected skill"
                )
                continue
            detail = _excerpt(binding.selection_reason, limit=140)
            self._log(
                "    "
                + _style("[task:skill]", ANSI_MAGENTA, bold=True)
                + f" {key} | {node.title} -> {binding.skill_name}"
                + (f" | {detail}" if detail else "")
            )

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


def _payload_chars(payload: object) -> int:
    return len(json.dumps(_jsonable(payload), ensure_ascii=False))


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _task_status_digest(snapshot: TaskTreeSnapshot) -> TaskStatusDigest:
    in_progress = [node for node in snapshot.nodes if node.status == TaskStatus.in_progress]
    completed = [node for node in snapshot.nodes if node.status == TaskStatus.completed]
    failed = [node for node in snapshot.nodes if node.status == TaskStatus.failed]
    parent_keys = {node.parent_key for node in snapshot.nodes if node.parent_key}
    leaf_nodes = [node for node in snapshot.nodes if node.key not in parent_keys]
    return TaskStatusDigest(
        in_progress=len(in_progress),
        completed=len(completed),
        failed=len(failed),
        leaf=len(leaf_nodes),
        in_progress_leaves=[_task_status_digest_item(item) for item in leaf_nodes if item.status == TaskStatus.in_progress][:8],
        failed_leaves=[_task_status_digest_item(item) for item in leaf_nodes if item.status == TaskStatus.failed][:5],
        completed_leaves=[_task_status_digest_item(item) for item in leaf_nodes if item.status == TaskStatus.completed][:5],
    )


def _task_status_digest_item(node: TaskNodeSnapshot) -> TaskStatusDigestItem:
    return TaskStatusDigestItem(
        key=node.key,
        title=node.title,
        status=node.status,
        attempt_count=node.attempt_count,
        latest_summary=node.latest_summary,
        latest_findings=list(node.latest_findings),
    )


def _failure_patterns_digest(reflection_history: list[ReflectionHistoryEntry]) -> list[FailurePattern]:
    merged: list[FailurePattern] = []
    seen: set[tuple[str, str]] = set()
    for entry in reversed(reflection_history):
        for item in reversed(entry.failure_patterns):
            key = (item.pattern, item.reason)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item.model_copy(deep=True))
            if len(merged) >= 8:
                return list(reversed(merged))
    return list(reversed(merged))


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
        summary=execution.summary,
        findings=list(execution.findings),
        stdout=execution.stdout,
        stderr=execution.stderr,
        command=execution.command,
        source=execution.source,
        failure_stage=execution.failure_stage,
        task_results={key: _compact_task_result(value) for key, value in execution.task_results.items()},
    )


def _compact_task_result(task_result: TaskExecutionResult) -> TaskExecutionResult:
    return task_result.model_copy(
        update={
            "summary": task_result.summary,
            "findings": list(task_result.findings),
            "stdout": task_result.stdout,
            "stderr": task_result.stderr,
            "command": task_result.command,
            "script_path": task_result.script_path,
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


def _available_tools_compact() -> list[AvailableTool]:
    tools: list[AvailableTool] = []
    for item in tool_inventory():
        input_schema = item.get("inputSchema") or {}
        properties = input_schema.get("properties") or {}
        required = set(input_schema.get("required") or [])
        optional = [name for name in properties if name not in required]
        tools.append(
            AvailableTool(
                name=str(item.get("name") or ""),
                summary=str(item.get("summary") or ""),
                server_name=str(item.get("server_name") or ""),
                required_args=[str(name) for name in required],
                optional_args=[str(name) for name in optional],
            )
        )
    return tools


def _contest_control_tools() -> list[AvailableTool]:
    return [
        AvailableTool(
            name="submit_flag",
            summary="向比赛平台提交一个候选 flag。只有在有明确证据支持且未被平台证伪时才应建议调用。",
            server_name="contest",
            required_args=["flag", "reason"],
            optional_args=[],
        ),
        AvailableTool(
            name="view_hint",
            summary="查看平台提示。仅当主线明显停滞且提示预计能改变下一轮计划时才应建议调用。",
            server_name="contest",
            required_args=["reason"],
            optional_args=[],
        ),
    ]


def _task_tree_focus(snapshot: TaskTreeSnapshot, task_key: str, *, limit: int) -> TaskTreeSnapshot:
    nodes_by_key = {node.key: node for node in snapshot.nodes}
    if task_key not in nodes_by_key:
        return TaskTreeSnapshot(nodes=[])

    selected: list[str] = []
    current = nodes_by_key[task_key]
    selected.append(current.key)
    cursor = current
    while cursor.parent_key and cursor.parent_key in nodes_by_key:
        cursor = nodes_by_key[cursor.parent_key]
        if cursor.key not in selected:
            selected.append(cursor.key)

    current_parent = current.parent_key
    if current_parent:
        siblings = [
            node for node in snapshot.nodes
            if node.parent_key == current_parent and node.key != task_key
            and (node.latest_findings or node.status != TaskStatus.completed or node.latest_summary)
        ]
        for sibling in siblings:
            if sibling.key not in selected:
                selected.append(sibling.key)
            if len(selected) >= limit:
                break

    if len(selected) < limit:
        for node in snapshot.nodes:
            if node.status == TaskStatus.in_progress and node.key not in selected:
                selected.append(node.key)
            if len(selected) >= limit:
                break

    ordered = [node.model_copy(deep=True) for node in snapshot.nodes if node.key in set(selected[:limit])]
    return TaskTreeSnapshot(nodes=ordered)


def _dependency_context(
    snapshot: TaskTreeSnapshot,
    task_key: str,
    available_artifacts: list[ArtifactRef],
    *,
    limit: int,
) -> list[TaskDependencyContext]:
    nodes_by_key = {node.key: node for node in snapshot.nodes}
    if task_key not in nodes_by_key:
        return []

    candidates: list[TaskNodeSnapshot] = []
    cursor = nodes_by_key[task_key]
    while cursor.parent_key and cursor.parent_key in nodes_by_key:
        cursor = nodes_by_key[cursor.parent_key]
        candidates.append(cursor)

    parent_key = nodes_by_key[task_key].parent_key
    if parent_key:
        for node in snapshot.nodes:
            if node.parent_key != parent_key or node.key == task_key:
                continue
            if node.latest_findings or node.status != TaskStatus.in_progress or node.latest_summary:
                candidates.append(node)

    artifacts = filter_available_artifacts(available_artifacts)
    seen: set[str] = set()
    context: list[TaskDependencyContext] = []
    for node in candidates:
        if node.key in seen:
            continue
        seen.add(node.key)
        related_artifacts = [
            item.model_copy(deep=True)
            for item in artifacts
            if item.producer_task_key == node.key
        ][:3]
        failure_reason = node.latest_summary if node.status == TaskStatus.failed else ""
        context.append(
            TaskDependencyContext(
                task_key=node.key,
                title=node.title,
                status=node.status,
                latest_summary=node.latest_summary,
                latest_findings=list(node.latest_findings),
                failure_reason=failure_reason,
                artifacts=related_artifacts,
            )
        )
        if len(context) >= limit:
            break
    return context


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


def _challenge_context(
    challenge: ChallengeSpec,
    *,
    submitted_flags: list[str] | None = None,
    incorrect_flags: list[str] | None = None,
    submission_history: list[SubmissionAttempt] | None = None,
) -> str:
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
    submitted_flags = [item for item in (submitted_flags or []) if item]
    incorrect_flags = [item for item in (incorrect_flags or []) if item]
    history = [item for item in (submission_history or []) if item.flag or item.message]
    if submitted_flags:
        lines.append("已确认正确Flag: " + ", ".join(submitted_flags[-6:]))
    if incorrect_flags:
        lines.append("已证伪Flag: " + ", ".join(incorrect_flags[-6:]))
    if history:
        lines.append("最近提交记录:")
        for item in history[-6:]:
            verdict = "correct" if item.correct else "wrong"
            progress = ""
            if item.progress_before or item.progress_after:
                progress = f" | progress={item.progress_before or '-'}->{item.progress_after or '-'}"
            lines.append(
                f"- cycle={item.cycle} | {verdict} | flag={item.flag} | reason={_excerpt(item.reason, limit=80)} | message={_excerpt(item.message, limit=120)}{progress}"
            )
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


def _persist_code_evidence_entries(
    *,
    cycle: int,
    task_node: TaskNodeSnapshot,
    evidence: list[CodeEvidence],
) -> list[PersistentCodeEvidence]:
    persisted: list[PersistentCodeEvidence] = []
    for item in evidence:
        snippet = item.snippet.strip()
        if not snippet:
            continue
        persisted.append(
            PersistentCodeEvidence(
                cycle=cycle,
                source_task_key=task_node.key,
                source_task_title=task_node.title,
                source=item.source,
                path_hint=item.path_hint,
                snippet=snippet,
                why_it_matters=item.why_it_matters,
                exploit_hint=item.exploit_hint,
                confidence=item.confidence,
            )
        )
    return persisted


def _merge_persistent_code_evidence(
    left: list[PersistentCodeEvidence],
    right: list[PersistentCodeEvidence],
    *,
    limit: int = 80,
) -> list[PersistentCodeEvidence]:
    merged: list[PersistentCodeEvidence] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in [*left, *right]:
        key = (
            item.source_task_key,
            item.path_hint,
            item.snippet,
            item.exploit_hint,
        )
        if key in seen:
            continue
        seen.add(key)
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


def _persistent_code_evidence_for_task(
    entries: list[PersistentCodeEvidence],
    snapshot: TaskTreeSnapshot,
    task_key: str,
    *,
    limit: int,
) -> list[PersistentCodeEvidence]:
    if not entries:
        return []

    nodes_by_key = {node.key: node for node in snapshot.nodes}
    ancestor_keys: set[str] = set()
    sibling_keys: set[str] = set()
    if task_key in nodes_by_key:
        cursor = nodes_by_key[task_key]
        ancestor_keys.add(task_key)
        while cursor.parent_key and cursor.parent_key in nodes_by_key:
            cursor = nodes_by_key[cursor.parent_key]
            ancestor_keys.add(cursor.key)
        parent_key = nodes_by_key[task_key].parent_key
        if parent_key:
            sibling_keys = {
                node.key
                for node in snapshot.nodes
                if node.parent_key == parent_key and node.key != task_key
            }

    def rank(item: PersistentCodeEvidence) -> tuple[int, int]:
        if item.source_task_key == task_key:
            return (0, -item.cycle)
        if item.source_task_key in ancestor_keys:
            return (1, -item.cycle)
        if item.source_task_key in sibling_keys:
            return (2, -item.cycle)
        return (9, -item.cycle)

    filtered = [item for item in entries if rank(item)[0] < 9]
    filtered.sort(key=rank)
    deduped: list[PersistentCodeEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in filtered:
        key = (item.source_task_key, item.path_hint, item.snippet)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item.model_copy(deep=True))
        if len(deduped) >= limit:
            break
    return deduped


def _load_skill_catalog(skills_dir: Path) -> list[SkillCard]:
    if not skills_dir.exists():
        return []

    catalog: list[SkillCard] = []
    for path in sorted(skills_dir.glob("*.md")):
        metadata, _ = _read_skill_markdown(path)
        name = metadata.get("name", path.stem).strip()
        if not name:
            continue
        catalog.append(
            SkillCard(
                name=name,
                summary=metadata.get("summary", "").strip(),
                use_when=metadata.get("use_when", "").strip(),
                source_path=str(path.resolve()),
            )
        )
    return catalog


def _read_skill_markdown(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    return _parse_simple_frontmatter(text)


def _parse_simple_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    metadata: dict[str, str] = {}
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()

    if closing_index is None:
        return {}, text.strip()

    body = "\n".join(lines[closing_index + 1 :]).strip()
    return metadata, body


def _task_signature(task_node: TaskNodeSnapshot) -> str:
    material = "||".join(
        [
            task_node.title.strip(),
            task_node.reason.strip(),
            task_node.completion_criteria.strip(),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _binding_matches_task(binding: TaskSkillBinding | None, task_node: TaskNodeSnapshot) -> bool:
    return binding is not None and binding.task_signature == _task_signature(task_node)


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
