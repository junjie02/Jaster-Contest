from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jaster.agents import build_agents
from jaster.domain import (
    ActionPlan,
    ArtifactRef,
    AttackTree,
    AttackTreeSnapshot,
    BuilderInput,
    ChallengeSpec,
    ExecutionResult,
    GlobalFacts,
    Observation,
    ReconInput,
    ReconOutput,
    ReflectionInput,
    RunState,
    SubmissionResult,
    StrategyInput,
    StrategyOutput,
    SubmissionInput,
)
from jaster.domain.models import TreeNodeSnapshot, NodeInfo
from pydantic import BaseModel, Field
from jaster.runtime.builder import BuilderExecutor
from jaster.runtime.env import env_int
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.skills import SkillCatalog, SkillExecutor
from jaster.storage.files import FileRunStore


class ExploitableNodeContext(BaseModel):
    """Context passed to strategy: target node + path to root + shared_refs nodes."""
    target_node: NodeInfo
    path_to_root: list[NodeInfo] = Field(default_factory=list)
    related_nodes: list[NodeInfo] = Field(default_factory=list)


class PendingObservation(BaseModel):
    round: int
    source: str
    execution: ExecutionResult


def _tree_node_to_info(node: TreeNodeSnapshot) -> NodeInfo:
    """Convert TreeNodeSnapshot to NodeInfo for strategy context"""
    return NodeInfo(
        key=node.key,
        parent_key=node.parent_key,
        title=node.title,
        kind=node.kind,
        locator=node.locator,
        status=node.status,
        priority=node.priority,
        value=node.value,
        reason=node.reason,
        how=node.how,
        evidence=list(node.evidence),
        shared_refs=list(node.shared_refs),
        key_findings=list(node.key_findings),
    )


def _extract_node_context(tree: AttackTree, selected_node_key: str) -> ExploitableNodeContext:
    """从树中提取：目标节点 + 路径到根节点 + shared_refs 关联节点"""
    nodes_by_key = {node.key: node for node in tree.snapshot().nodes}
    target_node = nodes_by_key.get(selected_node_key)
    if target_node is None:
        raise ValueError(f"Node key '{selected_node_key}' not found")

    # 收集目标节点到根节点的路径
    path_to_root = []
    current = target_node
    while current and current.parent_key:
        path_to_root.append(_tree_node_to_info(current))
        current = nodes_by_key.get(current.parent_key)
    if current:
        path_to_root.append(_tree_node_to_info(current))
    path_to_root.reverse()

    related_nodes = [_tree_node_to_info(nodes_by_key[ref]) for ref in target_node.shared_refs if ref in nodes_by_key]
    return ExploitableNodeContext(
        target_node=_tree_node_to_info(target_node),
        path_to_root=path_to_root,
        related_nodes=related_nodes,
    )


def _resolve_node_context(tree: AttackTree, selected_node_key: str) -> ExploitableNodeContext:
    snapshot = tree.snapshot()
    if selected_node_key and any(node.key == selected_node_key for node in snapshot.nodes):
        return _extract_node_context(tree, selected_node_key)
    if not snapshot.nodes:
        raise ValueError("Attack tree is empty")
    best_node = max(snapshot.nodes, key=lambda node: (node.priority, bool(node.parent_key)))
    return _extract_node_context(tree, best_node.key)


class JasterOrchestrator:
    def __init__(
        self,
        *,
        store: FileRunStore,
        prompt_root: Path,
        skills_dir: Path,
        llm: OpenAIChatClient,
        verbose: bool = True,
        on_tree_update: Callable[[object], None] | None = None,
    ) -> None:
        self.store = store
        self.prompt_root = prompt_root
        self.skill_catalog = SkillCatalog(skills_dir)
        self.skill_executor = SkillExecutor(self.skill_catalog)
        self.builder_executor = BuilderExecutor()
        self.agents = build_agents(prompt_root, llm)
        self.verbose = verbose
        self.phase_max_retries = env_int("JASTER_PHASE_MAX_RETRIES", 3)
        self._on_tree_update = on_tree_update
        self._last_builder_trace: dict | None = None

    def run(
        self,
        challenge: ChallengeSpec,
        *,
        max_rounds: int = 12,
        submission_handler: Callable[[ChallengeSpec, str, RunState], SubmissionResult] | None = None,
        round_hook: Callable[[RunState, str, ExecutionResult | None], bool] | None = None,
    ) -> RunState:
        run_id = self.store.new_run_id()
        tree = AttackTree.bootstrap(challenge.target)
        state = RunState(run_id=run_id, challenge=challenge, tree=tree.snapshot())
        self.store.create(state)
        self._log(f"[*] Run created: {run_id}")
        self._log(
            f"[*] Target: {challenge.target} | type={challenge.target_type} | zone={challenge.zone}"
        )
        self._log(f"[*] Run dir: {self.store.run_dir(run_id)}")

        latest_execution: ExecutionResult | None = None
        reflection_summary: str = ""
        recon_summary: str = ""
        strategy_summary: str = ""
        node_context: ExploitableNodeContext | None = None
        next_phase = "recon"
        phase_round = 0
        _reflection_entry: str = ""
        pending_observation: PendingObservation | None = None

        # HTTP 目标首次执行：curl 页面源码
        if state.challenge.target_type == "http":
            self._log(f"[*] Initial curl: {state.challenge.target}")
            import subprocess
            result = subprocess.run(
                ["curl", "-s", "-L", "--max-time", "30", state.challenge.target],
                capture_output=True,
                text=True,
                errors="replace",
            )
            latest_execution = ExecutionResult(
                success=result.returncode == 0,
                summary="Initial HTTP response captured" if result.stdout else "",
                findings=[],
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                command=f"curl -s -L {state.challenge.target}",
            )

        # === MAIN ORCHESTRATION LOOP ===
        while phase_round < max_rounds:
            phase_round += 1

            if next_phase == "recon":
                prev_execution = latest_execution
                recon_out, latest_execution, recon_elapsed = self._run_action_phase(
                    agent_name="recon",
                    zone=state.challenge.zone,
                    challenge=state.challenge,
                    run_id=run_id,
                    observations=state.observations[-50:],
                    latest_execution=prev_execution,
                    payload_factory=lambda execution: ReconInput(
                        objective=f"Recon the target {state.challenge.target} and expand the global attack tree.",
                        tree=_prompt_tree_snapshot(tree.snapshot()),
                        challenge_context=_challenge_context(state.challenge),
                        recent_observations=_compact_observations(state.observations[-50:]),
                        latest_execution=_compact_execution(execution),
                        available_skills=self.skill_catalog.list_available(),
                        latest_summary=strategy_summary,
                    ),
                    label=f"Round {phase_round}: recon",
                )
                self._log(f"    Phase time: {recon_elapsed:.2f}s")
                self._log(
                    f"    Action: {recon_out.action.kind}"
                    + (f" | skill={recon_out.action.skill_name}" if recon_out.action.skill_name else "")
                )
                tree.apply_patch(recon_out.tree_patch)

                self._log(
                    f"    Result: {'OK' if latest_execution.success else 'FAIL'}"
                    f" | {latest_execution.summary or '(no summary)'}"
                )
                _flush_pending_observation(state, pending_observation, recon_out.summary, recon_out.result_type)
                pending_observation = PendingObservation(
                    round=phase_round,
                    source="recon",
                    execution=latest_execution.model_copy(deep=True),
                )
                tree.merge_facts(_facts_from_execution(latest_execution))
                state.tree = tree.snapshot()
                self._append_phase_round(
                    run_id,
                    phase_round,
                    "recon",
                    {
                        "recon_input": _agent_trace(self.agents.get("recon")),
                        "recon": recon_out.model_dump(),
                        "builder_input": self._last_builder_trace,
                        "execution": latest_execution.model_dump(),
                    },
                )
                state.rounds_completed = phase_round
                self.store.save_state(state)
                self._notify_tree_update(state.tree)
                if round_hook and round_hook(state, "recon", latest_execution):
                    self.store.save_state(state)
                    self._notify_tree_update(state.tree)
                    self._log("[*] Run stopping: requested by round hook")
                    break

                if recon_out.discover_vulnerability or recon_out.action.kind == "finish":
                    self._log("[*] Recon complete: exploitable point found")
                    node_context = _resolve_node_context(tree, recon_out.selected_node_key)
                    recon_summary = recon_out.summary
                    _reflection_entry = "recon"
                    next_phase = "reflection"
                    continue

                # 每 5 轮强制 Reflection，对当前侦察方向做阶段性思考
                if phase_round > 0 and phase_round % 5 == 0:
                    self._log(f"[*] Periodic reflection: round {phase_round}")
                    recon_summary = recon_out.summary
                    _reflection_entry = "recon"
                    next_phase = "reflection"
                    continue

                continue

            if next_phase == "reflection":
                self._log("[*] Reflection: organizing findings for exploitable point")
                reflection_out, reflection_elapsed = self._timed_agent_run(
                    "reflection",
                    state.challenge.zone,
                    ReflectionInput(
                        objective="Reflect on the exploitable point found by recon, organize key findings, and provide strategic guidance.",
                        tree=_prompt_tree_snapshot(tree.snapshot()),
                        challenge_context=_challenge_context(state.challenge),
                        recent_observations=_compact_observations(state.observations[-50:]),
                        latest_execution=_compact_execution(latest_execution),
                        last_strategy=node_context.target_node.title if node_context else "",
                        latest_summary=recon_summary if _reflection_entry == "recon" else strategy_summary,
                    ),
                )
                self._log(f"    LLM time: {reflection_elapsed:.2f}s")
                reflection_summary = reflection_out.summary
                self._log(f"    Summary: {reflection_out.summary or '(empty)'}")
                _flush_pending_observation(state, pending_observation, reflection_out.summary, "")
                pending_observation = None
                tree.apply_patch(reflection_out.tree_patch)
                state.tree = tree.snapshot()
                # 用 reflection 的 next_focus_key 更新 node_context
                if reflection_out.next_focus_key:
                    try:
                        node_context = _resolve_node_context(tree, reflection_out.next_focus_key)
                    except ValueError:
                        pass  # 节点不存在，保持原 node_context
                self._append_phase_round(
                    run_id,
                    phase_round,
                    "reflection",
                    {
                        "reflection_input": _agent_trace(self.agents.get("reflection")),
                        "reflection": reflection_out.model_dump(),
                    },
                )
                state.rounds_completed = phase_round
                self.store.save_state(state)
                self._notify_tree_update(state.tree)
                if round_hook and round_hook(state, "reflection", latest_execution):
                    self.store.save_state(state)
                    self._notify_tree_update(state.tree)
                    self._log("[*] Run stopping: requested by round hook")
                    break
                if _reflection_entry == "recon":
                    next_phase = "strategy" if recon_out.discover_vulnerability else "recon"
                else:
                    next_phase = "recon" if strategy_out.need_recon else "strategy"
                continue

            if next_phase != "strategy":
                raise RuntimeError(f"Unknown phase: {next_phase}")

            prev_execution = latest_execution
            strategy_out, latest_execution, strategy_elapsed = self._run_action_phase(
                agent_name="strategy",
                zone=state.challenge.zone,
                challenge=state.challenge,
                run_id=run_id,
                observations=state.observations[-50:],
                latest_execution=prev_execution,
                payload_factory=lambda execution: StrategyInput(
                    objective=f"Exploit the target {state.challenge.target} and capture the flag.",
                    target_node=node_context.target_node,
                    path_to_root=node_context.path_to_root,
                    related_nodes=node_context.related_nodes,
                    challenge_context=_challenge_context(state.challenge),
                    latest_summary=reflection_summary,
                    recent_observations=_compact_observations(state.observations[-50:]),
                    latest_execution=_compact_execution(execution),
                    available_skills=self.skill_catalog.list_available(),
                ),
                label=f"Round {phase_round}: strategy",
            )
            self._log(f"    Phase time: {strategy_elapsed:.2f}s")
            self._log(
                f"    Action: {strategy_out.action.kind}"
                + (f" | skill={strategy_out.action.skill_name}" if strategy_out.action.skill_name else "")
            )
            tree.apply_patch(strategy_out.tree_patch)
            self._log(
                f"    Execution: {'OK' if latest_execution.success else 'FAIL'}"
                f" | {latest_execution.summary or '(no summary)'}"
            )
            _flush_pending_observation(state, pending_observation, strategy_out.summary, strategy_out.result_type)
            pending_observation = PendingObservation(
                round=phase_round,
                source="strategy",
                execution=latest_execution.model_copy(deep=True),
            )
            tree.merge_facts(_facts_from_execution(latest_execution))

            candidates = _merge_flag_candidates(strategy_out.flag_candidates, latest_execution.flag_candidates)
            submission_out = None
            if candidates:
                self._log(f"[*] Round {phase_round}: submission candidates={len(candidates)}")
                submission_out, submission_elapsed = self._timed_agent_run(
                    "submission",
                    state.challenge.zone,
                    SubmissionInput(
                        candidates=candidates,
                        recent_observations=_compact_observations(state.observations[-50:]),
                        submitted_flags=state.submitted_flags,
                    ),
                )
                self._log(f"    LLM time: {submission_elapsed:.2f}s")
                self._log(
                    f"    Submit: {'YES' if submission_out.should_submit else 'NO'}"
                    + (f" | flag={submission_out.flag}" if submission_out.flag else "")
                )
                if submission_out.should_submit and submission_out.flag and submission_out.flag not in state.submitted_flags:
                    if submission_handler:
                        submission_result = submission_handler(state.challenge, submission_out.flag, state)
                        self._log(
                            "    Platform submit: "
                            + ("OK" if submission_result.correct else "FAIL")
                            + (f" | {submission_result.message}" if submission_result.message else "")
                        )
                        state.challenge.flag_count = submission_result.flag_count or state.challenge.flag_count
                        state.challenge.flag_got_count = submission_result.flag_got_count or state.challenge.flag_got_count
                        if submission_result.correct:
                            state.submitted_flags.append(submission_out.flag)
                            tree.merge_facts(GlobalFacts(flags=[submission_out.flag]))
                    else:
                        state.submitted_flags.append(submission_out.flag)
                        tree.merge_facts(GlobalFacts(flags=[submission_out.flag]))
            else:
                self._log(f"[*] Round {phase_round}: submission skipped")

            state.tree = tree.snapshot()
            self._append_phase_round(
                run_id,
                phase_round,
                "strategy",
                {
                    "strategy_input": _agent_trace(self.agents.get("strategy")),
                    "strategy_context": {
                        "target_node": node_context.target_node.model_dump() if node_context else None,
                        "path_to_root": [n.model_dump() for n in node_context.path_to_root] if node_context else [],
                        "related_nodes": [n.model_dump() for n in node_context.related_nodes] if node_context else [],
                        "reflection_summary": reflection_summary,
                    },
                    "strategy": strategy_out.model_dump(),
                    "builder_input": self._last_builder_trace,
                    "execution": latest_execution.model_dump(),
                    "submission_input": _agent_trace(self.agents.get("submission")) if submission_out else None,
                    "submission": submission_out.model_dump() if submission_out else None,
                },
            )
            state.rounds_completed = phase_round
            self.store.save_state(state)
            self._notify_tree_update(state.tree)
            if round_hook and round_hook(state, "strategy", latest_execution):
                self.store.save_state(state)
                self._notify_tree_update(state.tree)
                self._log("[*] Run stopping: requested by round hook")
                break

            if strategy_out.goal_reached:
                self._log("[*] Run stopping: goal reached")
                break

            if strategy_out.need_recon:
                self._log("[*] Strategy requests more recon")
                strategy_summary = strategy_out.summary
                next_phase = "recon"
                continue

            # 每 5 轮强制 Reflection，对当前渗透方向做阶段性思考
            if phase_round > 0 and phase_round % 5 == 0:
                self._log(f"[*] Periodic reflection: round {phase_round}")
                strategy_summary = strategy_out.summary
                _reflection_entry = "strategy"
                next_phase = "reflection"
                continue

            next_phase = "strategy"

        self._log(
            f"[*] Run finished: rounds={state.rounds_completed} | submitted_flags={len(state.submitted_flags)}"
        )
        return state

    def _append_phase_round(self, run_id: str, round_num: int, agent_name: str, payload: dict[str, object]) -> None:
        body = {
            "round": round_num,
            "agent": agent_name,
            "phase": f"{agent_name}_round_{round_num}",
            **payload,
        }
        self.store.append_round(run_id, round_num, body)

    def _execute_action(
        self,
        *,
        run_id: str,
        challenge: ChallengeSpec,
        action: ActionPlan,
        observations: list[Observation],
        latest_execution: ExecutionResult | None,
    ) -> ExecutionResult:
        run_dir = self.store.run_dir(run_id)
        artifacts_dir = run_dir / "artifacts"
        work_dir = artifacts_dir / f"step-{len(list((run_dir / 'rounds').glob('*.json'))) + 1:03d}"
        self._last_builder_trace = None
        self._log(f"    Work dir: {work_dir}")
        if action.kind == "finish":
            self._log(f"    Finish action: {action.goal}")
            return ExecutionResult(success=True, summary=action.goal)
        if action.kind == "skill":
            self._log(
                f"    Running skill: {action.skill_name or '(unknown)'}"
                + (f" | args={action.skill_args}" if action.skill_args else "")
            )
            return self.skill_executor.run(action.skill_name or "", action.skill_args, cwd=artifacts_dir)
        self._log("    Calling builder LLM")
        builder_output, builder_elapsed = self._timed_agent_run(
            "builder",
            challenge.zone,
            BuilderInput(task=action.builder_task or action.goal),
        )
        self._last_builder_trace = _agent_trace(self.agents.get("builder"))
        self._log(f"    LLM time: {builder_elapsed:.2f}s")
        self._log(f"    Builder summary: {builder_output.summary or '(empty)'}")
        skills_dir = Path(getattr(self.skill_catalog, "skills_dir", self.prompt_root.parent.parent / "skills")).resolve()
        repo_root = skills_dir.parent.resolve()
        accessible_artifacts = [
            ArtifactRef(kind="run_dir", path=str(run_dir / "artifacts")),
            ArtifactRef(kind="repo_root", path=str(repo_root)),
            ArtifactRef(kind="skills_dir", path=str(skills_dir)),
        ]
        return self.builder_executor.run(
            builder_output,
            target=challenge.target,
            target_type=challenge.target_type,
            working_dir=work_dir,
            accessible_artifacts=accessible_artifacts,
            recent_observations=observations,
            latest_execution=latest_execution,
            repo_root=repo_root,
            skills_dir=skills_dir,
        )

    def _log(self, message: str) -> None:
        if getattr(self, "verbose", True):
            print(message, flush=True)

    def _notify_tree_update(self, tree_snapshot: AttackTreeSnapshot) -> None:
        callback = getattr(self, "_on_tree_update", None)
        if callback:
            callback(tree_snapshot)

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
        if retry_context is None:
            output = agent.run(zone, payload)
        else:
            try:
                output = agent.run(zone, payload, retry_context=retry_context)
            except TypeError as exc:
                if "retry_context" not in str(exc):
                    raise
                output = agent.run(zone, payload)
        return output, time.monotonic() - started

    def _run_action_phase(
        self,
        *,
        agent_name: str,
        zone: str,
        challenge: ChallengeSpec,
        run_id: str,
        observations: list[Observation],
        latest_execution: ExecutionResult | None,
        payload_factory: Callable[[ExecutionResult | None], object],
        label: str,
    ) -> tuple[ReconOutput | StrategyOutput, ExecutionResult, float]:
        agent = self.agents[agent_name]
        max_attempts = max(1, int(getattr(self, "phase_max_retries", 3) or 1))
        retry_context: dict[str, Any] | None = None
        current_execution = latest_execution
        total_elapsed = 0.0

        for attempt in range(1, max_attempts + 1):
            self._log(f"[*] {label}: phase attempt {attempt}/{max_attempts}")
            try:
                agent_out, agent_elapsed = self._timed_agent_run(
                    agent_name,
                    zone,
                    payload_factory(current_execution),
                    retry_context=retry_context,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"{agent_name} agent failed before action execution: {exc}. "
                    "This means the agent exhausted its own LLM/JSON retry budget before producing a valid action."
                ) from exc
            total_elapsed += agent_elapsed
            self._log(f"    \033[92m{agent_out.summary or '(empty)'}\033[0m")
            execution = self._execute_action(
                run_id=run_id,
                challenge=challenge,
                action=agent_out.action,
                observations=observations,
                latest_execution=current_execution,
            )
            execution.source = agent_name
            if execution.success or agent_out.action.kind == "finish":
                return agent_out, execution, total_elapsed
            current_execution = execution
            retry_context = _build_action_retry_context(
                attempt=attempt,
                max_attempts=max_attempts,
                action=agent_out.action,
                execution=execution,
            )
            self._log(
                f"    Action failed: {execution.summary or '(no summary)'}"
                f" | retrying current phase ({attempt}/{max_attempts})"
            )
        raise RuntimeError(
            f"{agent_name} phase failed after {max_attempts} attempts: "
            f"{current_execution.summary if current_execution else 'unknown error'}"
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


def _flush_pending_observation(
    state: RunState,
    pending: PendingObservation | None,
    summary: str,
    result_type: str,
) -> None:
    if pending is None:
        return
    state.observations.append(
        Observation(
            round=pending.round,
            source=pending.source,
            command=pending.execution.command,
            result_type=result_type,
            summary=summary,
        )
    )


def _facts_from_execution(result: ExecutionResult) -> GlobalFacts:
    credentials = re.findall(r"(?:password|token|secret|key)[:=]\s*(\S+)", "\n".join(result.findings), re.I)
    artifacts = [artifact.path for artifact in result.artifacts]
    return GlobalFacts(
        flags=result.flag_candidates,
        credentials=credentials,
        artifacts=artifacts,
    )


def _truncate_text(value: str, limit: int) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _compact_observations(observations: list[Observation], *, limit: int = 50) -> list[Observation]:
    return [item.model_copy() for item in observations[-limit:]]


def _compact_execution(execution: ExecutionResult | None) -> ExecutionResult | None:
    if execution is None:
        return None
    return ExecutionResult(
        success=execution.success,
        summary=execution.summary,
        findings=list(execution.findings),
        flag_candidates=list(execution.flag_candidates),
        artifacts=execution.artifacts[:3],
        stdout=execution.stdout,
        stderr=execution.stderr,
        exit_code=execution.exit_code,
        command=execution.command,
        script_path=execution.script_path,
        source=execution.source,
    )


def _prompt_tree_snapshot(snapshot: AttackTreeSnapshot) -> AttackTreeSnapshot:
    return AttackTreeSnapshot(
        nodes=list(snapshot.nodes),
        facts=GlobalFacts(
            flags=list(snapshot.facts.flags),
            credentials=list(snapshot.facts.credentials),
            services=list(snapshot.facts.services),
            artifacts=list(snapshot.facts.artifacts[-8:]),
        ),
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


def _merge_flag_candidates(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in merged:
                merged.append(item)
    return merged


def _agent_trace(agent: object) -> dict | None:
    trace = getattr(agent, "last_trace", None)
    return dict(trace) if isinstance(trace, dict) else None


def _build_action_retry_context(
    *,
    attempt: int,
    max_attempts: int,
    action: ActionPlan,
    execution: ExecutionResult,
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "failure_stage": "action_execution",
        "error_type": "ActionExecutionFailed",
        "error_message": execution.summary or execution.stderr or execution.stdout or "Action execution failed",
        "previous_action": action.model_dump(),
        "latest_execution": {
            "summary": execution.summary,
            "success": execution.success,
            "command": execution.command,
            "exit_code": execution.exit_code,
            "stdout_excerpt": _excerpt(execution.stdout),
            "stderr_excerpt": _excerpt(execution.stderr),
            "findings": execution.findings[:5],
        },
    }


def _excerpt(value: str, limit: int = 600) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."
