from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jaster.agents import build_agents
from jaster.agents.roles import BuilderAgent, ExecutorAgent
from jaster.domain import (
    ActionPlan,
    ArtifactRef,
    AttackTree,
    AttackTreeSnapshot,
    BuilderInput,
    ChallengeSpec,
    ExecutorInput,
    ExecutionResult,
    GlobalFacts,
    Observation,
    ObservedTaskResult,
    ReconInput,
    ReconOutput,
    RecentObservationAction,
    RecentObservationRound,
    ReflectionInput,
    RunState,
    SkillRouterInput,
    SubmissionResult,
    StrategyInput,
    StrategyOutput,
    SubmissionInput,
    TaskExecutionResult,
)
from jaster.domain.models import TreeNodeSnapshot, NodeInfo
from pydantic import BaseModel, Field
from jaster.runtime.env import env_int
from jaster.runtime.llm import LLMError, OpenAIChatClient
from jaster.runtime.catalog import FunctionExecutor, RuntimeCatalog, filter_available_artifacts
from jaster.runtime.builder import BuilderExecutor
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
    tasks: list[dict[str, str]] = Field(default_factory=list)


def _tree_node_to_info(node: TreeNodeSnapshot) -> NodeInfo:
    """Convert TreeNodeSnapshot to NodeInfo for strategy context"""
    return NodeInfo(
        key=node.key,
        parent_key=node.parent_key,
        title=node.title,
        kind=node.kind,
        status=node.status,
        priority=node.priority,
        reason=node.reason,
        how=node.how,
        shared_refs=list(node.shared_refs),
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
        functions_dir: Path,
        skills_dir: Path,
        llm: OpenAIChatClient,
        verbose: bool = True,
        on_tree_update: Callable[[object], None] | None = None,
    ) -> None:
        self.store = store
        self.prompt_root = prompt_root
        self.catalog = RuntimeCatalog(functions_dir, skills_dir)
        self.function_executor = FunctionExecutor(self.catalog)
        self.builder_executor = BuilderExecutor()
        self.agents = build_agents(prompt_root, llm)
        self.verbose = verbose
        self.phase_max_retries = env_int("JASTER_PHASE_MAX_RETRIES", 3)
        self.parallel_workers = env_int("JASTER_PARALLEL_FUNC_WORKERS", 4)
        self.prep_workers = env_int("JASTER_PREP_WORKERS", self.parallel_workers)
        self._on_tree_update = on_tree_update
        self._last_executor_trace: dict[str, dict[str, object]] = {}

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
                    run_id=run_id,
                    challenge=state.challenge,
                    latest_execution=prev_execution,
                    recent_observations=_compact_observations(state.observations),
                    payload_factory=lambda execution: ReconInput(
                        objective=f"Recon the target {state.challenge.target} and expand the global attack tree.",
                        tree=_prompt_tree_snapshot(tree.snapshot()),
                        challenge_context=_challenge_context(state.challenge),
                        recent_observations=_compact_observations(state.observations),
                        latest_execution=_compact_execution(execution),
                        available_artifacts=_prompt_artifacts(state.available_artifacts),
                        available_functions=self.catalog.list_functions(),
                        latest_summary=strategy_summary,
                    ),
                    label=f"Round {phase_round}: recon",
                )
                self._log(f"    Phase time: {recon_elapsed:.2f}s")
                self._log(
                    "    Actions: "
                    + ", ".join(_describe_action(action) for action in recon_out.actions)
                )
                tree.apply_patch(recon_out.tree_patch)

                self._log(
                    f"    Result: {'OK' if latest_execution.success else 'FAIL'}"
                    f" | {_execution_message(latest_execution)}"
                )
                _flush_pending_observation(state, pending_observation, recon_out.observed_task_results)
                pending_observation = PendingObservation(
                    round=phase_round,
                    source="recon",
                    execution=latest_execution.model_copy(deep=True),
                    tasks=_pending_observation_tasks(recon_out.actions),
                )
                state.available_artifacts = _merge_artifact_refs(
                    state.available_artifacts,
                    _available_artifacts(latest_execution.artifacts),
                )
                tree.merge_facts(_facts_from_execution(latest_execution))
                tree.merge_facts(GlobalFacts(credentials=recon_out.credentials))
                state.tree = tree.snapshot()
                self._append_phase_round(
                    run_id,
                    phase_round,
                    "recon",
                    {
                        "recon_input": _agent_trace(self.agents.get("recon")),
                        "recon": recon_out.model_dump(),
                        "executor_input": self._last_executor_trace,
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

                if recon_out.discover_vulnerability or _is_finish_only(recon_out.actions):
                    self._log("[*] Recon complete: exploitable point found")
                    node_context = _resolve_node_context(tree, recon_out.selected_node_key)
                    recon_summary = recon_out.phase_summary
                    _reflection_entry = "recon"
                    next_phase = "reflection"
                    continue

                # 每 5 轮强制 Reflection，对当前侦察方向做阶段性思考
                if phase_round > 0 and phase_round % 5 == 0:
                    self._log(f"[*] Periodic reflection: round {phase_round}")
                    recon_summary = recon_out.phase_summary
                    _reflection_entry = "recon"
                    next_phase = "reflection"
                    continue

                continue

            if next_phase == "reflection":
                self._log("[*] Reflection: organizing findings for exploitable point")
                skill_router_out = None
                skill_router_elapsed = 0.0
                skill_router_status = "skipped_no_skills"
                available_skills = self.catalog.list_skills()
                selected_skills: list[str] = []
                inspiration = ""
                if available_skills:
                    try:
                        skill_router_out, skill_router_elapsed = self._timed_agent_run(
                            "skill_router",
                            state.challenge.zone,
                            SkillRouterInput(
                                objective="Select 1-2 skills as inspiration before reflection.",
                                tree=_prompt_tree_snapshot(tree.snapshot()),
                                challenge_context=_challenge_context(state.challenge),
                                recent_observations=_compact_observations(state.observations),
                                latest_execution=_compact_execution(latest_execution),
                                available_artifacts=_prompt_artifacts(state.available_artifacts),
                                last_strategy=node_context.target_node.title if node_context else "",
                                latest_summary=recon_summary if _reflection_entry == "recon" else strategy_summary,
                                available_skills=available_skills,
                            ),
                        )
                        selected_skills = _normalize_selected_skills(
                            skill_router_out.selected_skills,
                            available={item.name for item in available_skills},
                        )
                        skill_router_status = "selected" if selected_skills else "fallback_empty_selection"
                    except Exception as exc:
                        skill_router_status = f"fallback_error:{type(exc).__name__}"
                        selected_skills = []
                    inspiration = self.catalog.render_inspiration(selected_skills)
                self._log(f"    Skill router time: {skill_router_elapsed:.2f}s | {skill_router_status}")
                reflection_out, reflection_elapsed = self._timed_agent_run(
                    "reflection",
                    state.challenge.zone,
                    ReflectionInput(
                        objective="Reflect on the exploitable point found by recon, organize key findings, and provide strategic guidance.",
                        tree=_prompt_tree_snapshot(tree.snapshot()),
                        challenge_context=_challenge_context(state.challenge),
                        recent_observations=_compact_observations(state.observations),
                        latest_execution=_compact_execution(latest_execution),
                        available_artifacts=_prompt_artifacts(state.available_artifacts),
                        last_strategy=node_context.target_node.title if node_context else "",
                        latest_summary=recon_summary if _reflection_entry == "recon" else strategy_summary,
                        selected_skills=selected_skills,
                        inspiration=inspiration,
                    ),
                )
                self._log(f"    LLM time: {reflection_elapsed:.2f}s")
                reflection_summary = reflection_out.summary
                self._log(f"    Summary: {reflection_out.summary or '(empty)'}")
                tree.apply_patch(reflection_out.tree_patch)
                tree.merge_facts(GlobalFacts(credentials=reflection_out.credentials))
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
                        "skill_router_input": _agent_trace(self.agents.get("skill_router")),
                        "skill_router": skill_router_out.model_dump() if skill_router_out else None,
                        "skill_router_status": skill_router_status,
                        "selected_skills": selected_skills,
                        "inspiration": inspiration,
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
                run_id=run_id,
                challenge=state.challenge,
                latest_execution=prev_execution,
                recent_observations=_compact_observations(state.observations),
                payload_factory=lambda execution: StrategyInput(
                    objective=f"Exploit the target {state.challenge.target} and capture the flag.",
                    target_node=node_context.target_node,
                    path_to_root=node_context.path_to_root,
                    related_nodes=node_context.related_nodes,
                    challenge_context=_challenge_context(state.challenge),
                    latest_summary=reflection_summary,
                    recent_observations=_compact_observations(state.observations),
                    latest_execution=_compact_execution(execution),
                    available_artifacts=_prompt_artifacts(state.available_artifacts),
                    available_functions=self.catalog.list_functions(),
                ),
                label=f"Round {phase_round}: strategy",
            )
            self._log(f"    Phase time: {strategy_elapsed:.2f}s")
            self._log(
                "    Actions: "
                + ", ".join(_describe_action(action) for action in strategy_out.actions)
            )
            tree.apply_patch(strategy_out.tree_patch)
            self._log(
                f"    Execution: {'OK' if latest_execution.success else 'FAIL'}"
                f" | {_execution_message(latest_execution)}"
            )
            _flush_pending_observation(state, pending_observation, strategy_out.observed_task_results)
            pending_observation = PendingObservation(
                round=phase_round,
                source="strategy",
                execution=latest_execution.model_copy(deep=True),
                tasks=_pending_observation_tasks(strategy_out.actions),
            )
            state.available_artifacts = _merge_artifact_refs(
                state.available_artifacts,
                _available_artifacts(latest_execution.artifacts),
            )
            tree.merge_facts(_facts_from_execution(latest_execution))
            tree.merge_facts(GlobalFacts(credentials=strategy_out.credentials))

            candidates = _merge_flag_candidates(strategy_out.flag_candidates, latest_execution.flag_candidates)
            submission_out = None
            if candidates:
                self._log(f"[*] Round {phase_round}: submission candidates={len(candidates)}")
                submission_out, submission_elapsed = self._timed_agent_run(
                    "submission",
                    state.challenge.zone,
                    SubmissionInput(
                        candidates=candidates,
                        recent_observations=_compact_observations(state.observations),
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
                    "executor_input": self._last_executor_trace,
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
                strategy_summary = strategy_out.phase_summary
                next_phase = "recon"
                continue

            # 每 5 轮强制 Reflection，对当前渗透方向做阶段性思考
            if phase_round > 0 and phase_round % 5 == 0:
                self._log(f"[*] Periodic reflection: round {phase_round}")
                strategy_summary = strategy_out.phase_summary
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

    def _execute_actions(
        self,
        *,
        run_id: str,
        agent_name: str,
        zone: str,
        actions: list[ActionPlan],
        challenge: ChallengeSpec | None = None,
        recent_observations: list[RecentObservationRound] | None = None,
        latest_execution: ExecutionResult | None = None,
        available_artifacts: list[ArtifactRef] | None = None,
    ) -> ExecutionResult:
        run_dir = self.store.run_dir(run_id)
        batch_dir = run_dir / "artifacts" / f"{agent_name}_{time.time_ns()}"
        self._last_executor_trace = {}
        self._log(f"    Work dir: {batch_dir}")
        if _is_finish_only(actions):
            finish_action = actions[0]
            self._log(f"    Finish action: {finish_action.goal}")
            task_result = TaskExecutionResult(
                task_id=finish_action.task_id,
                kind="finish",
                success=True,
                summary=finish_action.goal,
                source=agent_name,
            )
            return _build_batch_execution(agent_name, [task_result])

        immediate_results: list[TaskExecutionResult] = []
        for action in actions:
            if not action.function_name:
                if action.kind != "builder":
                    immediate_results.append(
                        TaskExecutionResult(
                            task_id=action.task_id,
                            kind=action.kind,
                            success=False,
                            summary="Missing function_name",
                            stderr="missing function_name",
                            source=agent_name,
                            failure_stage="executor_tool_call",
                        )
                    )
                continue

        task_results_by_id: dict[str, TaskExecutionResult] = {result.task_id: result for result in immediate_results}
        runnable_actions = [action for action in actions if action.task_id not in task_results_by_id]
        if runnable_actions:
            prep_workers = max(1, min(self.prep_workers, len(runnable_actions)))
            exec_workers = max(1, min(self.parallel_workers, len(runnable_actions)))
            with ThreadPoolExecutor(max_workers=prep_workers) as prepare_pool, ThreadPoolExecutor(
                max_workers=exec_workers
            ) as execution_pool:
                prepare_futures = {
                    prepare_pool.submit(
                        self._prepare_action,
                        action=action,
                        agent_name=agent_name,
                        zone=zone,
                        challenge=challenge,
                        latest_execution=latest_execution,
                        available_artifacts=available_artifacts or [],
                        recent_observations=recent_observations or [],
                        batch_dir=batch_dir,
                    ): action
                    for action in runnable_actions
                }
                execution_futures: dict[object, ActionPlan] = {}

                for prepare_future in as_completed(prepare_futures):
                    action = prepare_futures[prepare_future]
                    prepared = prepare_future.result()
                    self._last_executor_trace[action.task_id] = prepared["trace"]
                    task_result = prepared["task_result"]
                    if task_result is not None:
                        task_results_by_id[action.task_id] = task_result
                        continue
                    self._log(f"    Submitting execution: {action.task_id}")
                    execution_futures[execution_pool.submit(prepared["run_callable"])] = action

                for execution_future in as_completed(execution_futures):
                    action = execution_futures[execution_future]
                    try:
                        result = execution_future.result()
                    except Exception as exc:
                        result = ExecutionResult(
                            success=False,
                            summary=f"Unhandled execution error for {action.task_id}",
                            stderr=str(exc),
                            failure_stage="function_execution",
                            source=agent_name,
                        )
                    task_result = _to_task_result(action, result, source=agent_name)
                    self._log(
                        f"    Execution finished: {action.task_id} | {'OK' if task_result.success else 'FAIL'}"
                    )
                    task_results_by_id[action.task_id] = task_result

        ordered_task_results = [task_results_by_id[action.task_id] for action in actions if action.task_id in task_results_by_id]
        return _build_batch_execution(agent_name, ordered_task_results)

    def _prepare_action(
        self,
        *,
        action: ActionPlan,
        agent_name: str,
        zone: str,
        challenge: ChallengeSpec | None,
        latest_execution: ExecutionResult | None,
        available_artifacts: list[ArtifactRef],
        recent_observations: list[RecentObservationRound],
        batch_dir: Path,
    ) -> dict[str, object]:
        task_dir = batch_dir / action.task_id
        if action.kind == "builder":
            self._log(f"    Preparing builder: {action.task_id}")
            agent = self._new_builder_agent()
            try:
                started = time.monotonic()
                builder_out = agent.run(
                    zone,
                    BuilderInput(
                        task=action.executor_brief or action.goal,
                        key_parameters=action.key_parameters,
                        accessible_artifacts=list(available_artifacts),
                    ),
                )
                self._log(
                    f"    Prepared builder: {action.task_id} | elapsed={time.monotonic() - started:.2f}s"
                )
            except Exception as exc:
                return {
                    "trace": _agent_trace(agent) or {},
                    "task_result": TaskExecutionResult(
                        task_id=action.task_id,
                        kind="builder",
                        success=False,
                        summary="Builder agent failed",
                        stderr=str(exc),
                        source=agent_name,
                        failure_stage="builder_generation",
                    ),
                    "run_callable": None,
                }
            return {
                "trace": _agent_trace(agent) or {},
                "task_result": None,
                "run_callable": lambda builder_out=builder_out, task_dir=task_dir: self.builder_executor.run(
                    builder_out,
                    target=challenge.target if challenge else "",
                    target_type=challenge.target_type if challenge else "http",
                    working_dir=task_dir,
                    accessible_artifacts=list(available_artifacts),
                    recent_observations=list(recent_observations),
                    latest_execution=latest_execution,
                    repo_root=self.catalog.functions_dir.parent,
                    skills_dir=self.catalog.skills_dir,
                ),
            }

        function_spec = self.catalog.get_function(action.function_name or "")
        if function_spec is None:
            return {
                "trace": {},
                "task_result": TaskExecutionResult(
                    task_id=action.task_id,
                    kind=action.kind,
                    function_name=action.function_name,
                    success=False,
                    summary=f"Unknown function: {action.function_name}",
                    stderr="unknown function",
                    source=agent_name,
                    failure_stage="executor_tool_call",
                ),
                "run_callable": None,
            }

        self._log(f"    Preparing function: {action.function_name} ({action.task_id})")
        agent = self._new_executor_agent()
        try:
            started = time.monotonic()
            tool_call = agent.run(
                zone,
                ExecutorInput(
                    target=challenge.target if challenge else "",
                    function_name=action.function_name or "",
                    function_summary=function_spec.summary,
                    function_schema_text=self.catalog.tool_prompt_text(action.function_name or ""),
                    function_definition_json=self.catalog.get_function_definition_text(action.function_name or ""),
                    executor_brief=action.executor_brief,
                    accessible_artifacts=list(available_artifacts),
                ),
                tool_name=action.function_name or "",
                tools=[self.catalog.build_tool(action.function_name or "")],
            )
            self._log(
                f"    Prepared function: {action.function_name} ({action.task_id}) | elapsed={time.monotonic() - started:.2f}s"
            )
        except Exception as exc:
            raw_text = exc.raw_text if isinstance(exc, LLMError) else ""
            return {
                "trace": _agent_trace(agent) or {},
                "task_result": TaskExecutionResult(
                    task_id=action.task_id,
                    kind=action.kind,
                    function_name=action.function_name,
                    success=False,
                    summary=f"Executor failed for {action.function_name}",
                    findings=[raw_text] if raw_text else [],
                    stderr=str(exc),
                    source=agent_name,
                    failure_stage="executor_tool_call",
                ),
                "run_callable": None,
            }

        function_args = dict(tool_call.get("arguments") or {})
        action.function_args = function_args
        return {
            "trace": _agent_trace(agent) or {},
            "task_result": None,
            "run_callable": lambda action=action, function_args=function_args, task_dir=task_dir: self.function_executor.run_function(
                action.function_name or "",
                function_args,
                cwd=task_dir,
            ),
        }

    def _new_executor_agent(self) -> ExecutorAgent:
        current = self.agents["executor"]
        return ExecutorAgent(current.llm, current.prompts)

    def _new_builder_agent(self) -> BuilderAgent:
        current = self.agents["builder"]
        return BuilderAgent(current.llm, current.prompts)

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
        run_id: str,
        challenge: ChallengeSpec | None = None,
        latest_execution: ExecutionResult | None,
        recent_observations: list[RecentObservationRound] | None = None,
        payload_factory: Callable[[ExecutionResult | None], object],
        label: str,
    ) -> tuple[ReconOutput | StrategyOutput, ExecutionResult, float]:
        max_attempts = max(1, int(getattr(self, "phase_max_retries", 3) or 1))
        retry_context: dict[str, Any] | None = None
        current_execution = latest_execution
        total_elapsed = 0.0

        for attempt in range(1, max_attempts + 1):
            self._log(f"[*] {label}: phase attempt {attempt}/{max_attempts}")
            payload = payload_factory(current_execution)
            try:
                agent_out, agent_elapsed = self._timed_agent_run(
                    agent_name,
                    zone,
                    payload,
                    retry_context=retry_context,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"{agent_name} agent failed before action execution: {exc}. "
                    "This means the agent exhausted its own LLM/JSON retry budget before producing a valid action."
                ) from exc
            total_elapsed += agent_elapsed
            self._log(f"    \033[92m{agent_out.phase_summary or '(empty)'}\033[0m")
            execution = self._execute_actions(
                run_id=run_id,
                agent_name=agent_name,
                zone=zone,
                actions=agent_out.actions,
                challenge=challenge,
                recent_observations=recent_observations or [],
                latest_execution=current_execution,
                available_artifacts=(list(payload.available_artifacts) if hasattr(payload, "available_artifacts") else []),
            )
            execution.source = agent_name
            if execution.success or _is_finish_only(agent_out.actions):
                return agent_out, execution, total_elapsed
            current_execution = execution
            retry_context = _build_action_retry_context(
                attempt=attempt,
                max_attempts=max_attempts,
                actions=agent_out.actions,
                execution=execution,
            )
            self._log(
                f"    Action failed: {_execution_message(execution)}"
                f" | retrying current phase ({attempt}/{max_attempts})"
            )
        raise RuntimeError(
            f"{agent_name} phase failed after {max_attempts} attempts: "
            f"{_execution_message(current_execution) if current_execution else 'unknown error'}"
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
    observed_task_results: list[ObservedTaskResult],
) -> None:
    if pending is None:
        return
    if not pending.tasks:
        return
    by_task_id = {
        item.task_id: item
        for item in observed_task_results
        if item.task_id and (item.target or item.result)
    }
    missing = [item["task_id"] for item in pending.tasks if item["task_id"] not in by_task_id]
    if missing:
        raise ValueError(f"Missing observed_task_results for task ids: {', '.join(missing)}")
    for task in pending.tasks:
        observed = by_task_id[task["task_id"]]
        state.observations.append(
            Observation(
                round=pending.round,
                source=pending.source,
                task_id=task["task_id"],
                task=task["task"],
                target=observed.target,
                result=observed.result,
            )
        )


def _facts_from_execution(result: ExecutionResult) -> GlobalFacts:
    return GlobalFacts(
        flags=result.flag_candidates,
    )


def _compact_observations(
    observations: list[Observation],
    *,
    limit: int = 50,
) -> list[RecentObservationRound]:
    grouped = _serialize_recent_observations(observations)
    return [item.model_copy(deep=True) for item in grouped[-limit:]]


def _serialize_recent_observations(observations: list[Observation]) -> list[RecentObservationRound]:
    rounds: dict[int, list[RecentObservationAction]] = {}
    ordered_rounds: list[int] = []
    for item in observations:
        round_num = item.round
        if round_num not in rounds:
            rounds[round_num] = []
            ordered_rounds.append(round_num)
        action = RecentObservationAction(
            task=item.task,
            target=item.target,
            result=item.result,
        )
        rounds[round_num].append(action)
    serialized: list[RecentObservationRound] = []
    for round_num in ordered_rounds:
        serialized.append(
            RecentObservationRound(
                round=round_num,
                actions=rounds[round_num],
            )
        )
    return serialized


def _compact_execution(execution: ExecutionResult | None) -> ExecutionResult | None:
    if execution is None:
        return None
    return ExecutionResult(
        success=execution.success,
        batch_status=execution.batch_status,
        source=execution.source,
        failure_stage=execution.failure_stage,
        task_results={key: value.model_copy(deep=True) for key, value in execution.task_results.items()},
    )


def _prompt_artifacts(artifacts: list[ArtifactRef], *, limit: int = 20) -> list[ArtifactRef]:
    filtered = filter_available_artifacts(artifacts)
    return [item.model_copy(deep=True) for item in filtered[-limit:]]


def _prompt_tree_snapshot(snapshot: AttackTreeSnapshot) -> AttackTreeSnapshot:
    return AttackTreeSnapshot(
        nodes=list(snapshot.nodes),
        facts=GlobalFacts(
            flags=list(snapshot.facts.flags),
            credentials=list(snapshot.facts.credentials),
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


def _normalize_selected_skills(selected: list[str], *, available: set[str]) -> list[str]:
    normalized: list[str] = []
    for item in selected:
        name = str(item).strip()
        if not name or name not in available or name in normalized:
            continue
        normalized.append(name)
        if len(normalized) >= 2:
            break
    return normalized


def _build_action_retry_context(
    *,
    attempt: int,
    max_attempts: int,
    actions: list[ActionPlan],
    execution: ExecutionResult,
) -> dict[str, Any]:
    task_failures = [
        {
            "task_id": item.task_id,
            "success": item.success,
            "summary": item.summary,
            "exit_code": item.exit_code,
            "failure_stage": item.failure_stage,
            "stdout_excerpt": _excerpt(item.stdout),
            "stderr_excerpt": _excerpt(item.stderr),
        }
        for item in execution.task_results.values()
    ]
    return {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "failure_stage": execution.failure_stage or "action_execution",
        "error_type": "ActionExecutionFailed",
        "error_message": _execution_message(execution),
        "previous_actions": [action.model_dump() for action in actions],
        "latest_execution": {
            "success": execution.success,
            "batch_status": execution.batch_status,
            "source": execution.source,
            "failure_stage": execution.failure_stage,
            "task_results": task_failures,
        },
    }


def _excerpt(value: str, limit: int = 600) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _describe_action(action: ActionPlan) -> str:
    label = f"{action.task_id}:{action.kind}"
    if action.function_name:
        label += f"({action.function_name})"
    return label


def _task_name_from_action(action: ActionPlan) -> str:
    if action.kind == "function" and action.function_name:
        return action.function_name
    if action.kind == "builder":
        return "builder"
    if action.kind == "finish":
        return "finish"
    return action.kind


def _pending_observation_tasks(actions: list[ActionPlan]) -> list[dict[str, str]]:
    return [
        {
            "task_id": action.task_id,
            "task": _task_name_from_action(action),
        }
        for action in actions
    ]


def _execution_message(execution: ExecutionResult) -> str:
    for item in execution.task_results.values():
        if item.summary:
            return f"[{item.task_id}] {item.summary}"
        if item.stderr.strip():
            return f"[{item.task_id}] {item.stderr.strip().splitlines()[0]}"
        if item.stdout.strip():
            return f"[{item.task_id}] {item.stdout.strip().splitlines()[0]}"
    return "Action execution failed"


def _is_finish_only(actions: list[ActionPlan]) -> bool:
    return len(actions) == 1 and actions[0].kind == "finish"


def _to_task_result(action: ActionPlan, execution: ExecutionResult, *, source: str) -> TaskExecutionResult:
    return TaskExecutionResult(
        task_id=action.task_id,
        kind=action.kind,
        function_name=action.function_name,
        success=execution.success,
        summary=execution.summary,
        findings=list(execution.findings),
        flag_candidates=list(execution.flag_candidates),
        artifacts=_annotate_artifacts(
            execution.artifacts,
            producer_phase=source,
            producer_task_id=action.task_id,
            producer_function_name=action.function_name or action.kind,
            producer_success=execution.success,
        ),
        stdout=execution.stdout,
        stderr=execution.stderr,
        exit_code=execution.exit_code,
        command=execution.command,
        script_path=execution.script_path,
        source=source,
        failure_stage=execution.failure_stage,
    )


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
    commands = [f"[{item.task_id}] {item.command}" for item in task_results if item.command]
    stdout_parts = [f"[{item.task_id}]\n{item.stdout}" for item in task_results if item.stdout]
    stderr_parts = [f"[{item.task_id}]\n{item.stderr}" for item in task_results if item.stderr]

    findings: list[str] = []
    flag_candidates: list[str] = []
    artifacts: list[ArtifactRef] = []
    for item in task_results:
        for finding in item.findings:
            if finding not in findings:
                findings.append(finding)
        for flag in item.flag_candidates:
            if flag not in flag_candidates:
                flag_candidates.append(flag)
        artifacts = _merge_artifact_refs(artifacts, item.artifacts)

    failure_stage = ""
    if batch_status == "full_fail":
        failure_stage = next((item.failure_stage for item in task_results if item.failure_stage), "action_execution")

    return ExecutionResult(
        success=success,
        batch_status=batch_status,
        summary="\n".join(summaries),
        findings=findings,
        flag_candidates=flag_candidates,
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
    producer_task_id: str,
    producer_function_name: str,
    producer_success: bool,
) -> list[ArtifactRef]:
    annotated: list[ArtifactRef] = []
    for artifact in artifacts:
        annotated.append(
            artifact.model_copy(
                update={
                    "producer_phase": producer_phase,
                    "producer_task_id": producer_task_id,
                    "producer_function_name": producer_function_name,
                    "producer_success": producer_success,
                }
            )
        )
    return _merge_artifact_refs([], annotated)


def _available_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    return filter_available_artifacts(artifacts)


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
