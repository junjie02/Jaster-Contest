from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from jaster.agents import build_agents
from jaster.domain import (
    ActionPlan,
    ArtifactRef,
    AttackTree,
    BuilderInput,
    ChallengeSpec,
    ExecutionResult,
    GlobalFacts,
    Observation,
    ReconInput,
    ReconOutput,
    ReflectionInput,
    RunState,
    StrategyInput,
    StrategyOutput,
    SubmissionInput,
)
from jaster.runtime.builder import BuilderExecutor
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.skills import SkillCatalog, SkillExecutor
from jaster.storage.files import FileRunStore


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
        self._on_tree_update = on_tree_update
        self._last_builder_trace: dict | None = None

    def run(self, challenge: ChallengeSpec, *, max_recon_steps: int = 3, max_rounds: int = 12) -> RunState:
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
        last_reflection = ""

        for recon_index in range(1, max_recon_steps + 1):
            # 保存上一轮 execution，用于创建上一轮的 observation
            prev_execution = latest_execution

            self._log(f"[*] Recon step {recon_index}/{max_recon_steps}: calling LLM")
            recon_out, recon_elapsed = self._timed_agent_run(
                "recon",
                challenge.zone,
                ReconInput(
                    objective=f"Recon the target {challenge.target} and expand the global attack tree.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-6:],
                    latest_execution=prev_execution,
                    available_skills=self.skill_catalog.list_available(),
                ),
            )
            self._log(f"    LLM time: {recon_elapsed:.2f}s")
            self._log(f"    Summary: {recon_out.summary or '(empty)'}")
            self._log(
                f"    Action: {recon_out.action.kind}"
                + (f" | skill={recon_out.action.skill_name}" if recon_out.action.skill_name else "")
            )
            tree.apply_patch(recon_out.tree_patch)
            latest_execution = self._execute_action(
                run_id=run_id,
                challenge=challenge,
                action=recon_out.action,
                observations=state.observations[-6:],
                latest_execution=prev_execution,
            )
            self._log(
                f"    Result: {'OK' if latest_execution.success else 'FAIL'}"
                f" | {latest_execution.summary or '(no summary)'}"
            )
            # 创建上一轮的 observation：round_num = recon_index - 1
            state.observations.append(_create_observation(recon_index - 1, "recon", prev_execution, recon_out))
            tree.merge_facts(_facts_from_execution(latest_execution))
            state.tree = tree.snapshot()
            self.store.append_round(
                run_id,
                recon_index,
                {
                    "phase": "recon",
                    "recon_input": _agent_trace(self.agents.get("recon")),
                    "recon": recon_out.model_dump(),
                    "builder_input": self._last_builder_trace,
                    "execution": latest_execution.model_dump(),
                },
            )
            self.store.save_state(state)
            self._notify_tree_update(state.tree)
            if recon_out.done or recon_out.action.kind == "finish":
                self._log("[*] Recon complete")
                break

        for round_index in range(1, max_rounds + 1):
            # 保存上一轮 execution，用于创建上一轮的 observation
            prev_execution = latest_execution

            self._log(f"[*] Main round {round_index}/{max_rounds}: strategy")
            strategy_out, strategy_elapsed = self._timed_agent_run(
                "strategy",
                challenge.zone,
                StrategyInput(
                    objective=f"Exploit the target {challenge.target} and capture the flag.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-8:],
                    latest_execution=prev_execution,
                    last_reflection=last_reflection,
                ),
            )
            self._log(f"    LLM time: {strategy_elapsed:.2f}s")
            selected_key = strategy_out.selected_node_key or (state.tree.frontier_keys[0] if state.tree.frontier_keys else "")
            self._log(f"    Summary: {strategy_out.summary or '(empty)'}")
            self._log(
                f"    Selected node: {selected_key or '(none)'}"
            )
            self._log(
                f"    Action: {strategy_out.action.kind}"
                + (f" | skill={strategy_out.action.skill_name}" if strategy_out.action.skill_name else "")
            )
            if selected_key:
                tree.set_selected_node(selected_key)
            strategy_out.tree_patch.selected_node_key = selected_key or strategy_out.tree_patch.selected_node_key
            tree.apply_patch(strategy_out.tree_patch)
            latest_execution = self._execute_action(
                run_id=run_id,
                challenge=challenge,
                action=strategy_out.action,
                observations=state.observations[-8:],
                latest_execution=prev_execution,
            )
            self._log(
                f"    Execution: {'OK' if latest_execution.success else 'FAIL'}"
                f" | {latest_execution.summary or '(no summary)'}"
            )
            # 创建上一轮的 observation：round_num = max_recon_steps + round_index - 1
            state.observations.append(_create_observation(max_recon_steps + round_index - 1, "strategy", prev_execution, strategy_out))
            tree.merge_facts(_facts_from_execution(latest_execution))

            self._log(f"[*] Main round {round_index}/{max_rounds}: reflection")
            reflection_out, reflection_elapsed = self._timed_agent_run(
                "reflection",
                challenge.zone,
                ReflectionInput(
                    objective="Reflect on the latest action, correct drift, and update the global attack tree.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-8:],
                    latest_execution=latest_execution,
                    last_strategy=strategy_out.summary,
                ),
            )
            self._log(f"    LLM time: {reflection_elapsed:.2f}s")
            last_reflection = reflection_out.summary
            self._log(f"    Summary: {reflection_out.summary or '(empty)'}")
            self._log(f"    Next focus: {reflection_out.next_focus_key or '(unchanged)'}")
            tree.apply_patch(reflection_out.tree_patch)
            if reflection_out.next_focus_key:
                tree.set_selected_node(reflection_out.next_focus_key)

            candidates = _merge_flag_candidates(strategy_out.flag_candidates, latest_execution.flag_candidates, reflection_out.flag_candidates)
            submission_out = None
            if candidates:
                self._log(f"[*] Main round {round_index}/{max_rounds}: submission candidates={len(candidates)}")
                submission_out, submission_elapsed = self._timed_agent_run(
                    "submission",
                    challenge.zone,
                    SubmissionInput(
                        candidates=candidates,
                        recent_observations=state.observations[-5:],
                        submitted_flags=state.submitted_flags,
                    ),
                )
                self._log(f"    LLM time: {submission_elapsed:.2f}s")
                self._log(
                    f"    Submit: {'YES' if submission_out.should_submit else 'NO'}"
                    + (f" | flag={submission_out.flag}" if submission_out.flag else "")
                )
                if submission_out.should_submit and submission_out.flag and submission_out.flag not in state.submitted_flags:
                    state.submitted_flags.append(submission_out.flag)
                    tree.merge_facts(GlobalFacts(flags=[submission_out.flag]))
            else:
                self._log(f"[*] Main round {round_index}/{max_rounds}: submission skipped")

            state.rounds_completed += 1
            state.tree = tree.snapshot()
            self.store.append_round(
                run_id,
                max_recon_steps + round_index,
                {
                    "phase": "main",
                    "strategy_input": _agent_trace(self.agents.get("strategy")),
                    "strategy": strategy_out.model_dump(),
                    "builder_input": self._last_builder_trace,
                    "execution": latest_execution.model_dump(),
                    "reflection_input": _agent_trace(self.agents.get("reflection")),
                    "reflection": reflection_out.model_dump(),
                    "submission_input": _agent_trace(self.agents.get("submission")) if submission_out else None,
                    "submission": submission_out.model_dump() if submission_out else None,
                },
            )
            self.store.save_state(state)
            self._notify_tree_update(state.tree)
            if strategy_out.goal_reached or reflection_out.halt:
                self._log("[*] Run stopping: goal reached or reflection requested halt")
                break
        self._log(
            f"[*] Run finished: rounds={state.rounds_completed} | submitted_flags={len(state.submitted_flags)}"
        )
        return state

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
        accessible_artifacts = [ArtifactRef(kind="run_dir", path=str(run_dir / "artifacts"))]
        return self.builder_executor.run(
            builder_output,
            target=challenge.target,
            target_type=challenge.target_type,
            working_dir=work_dir,
            accessible_artifacts=accessible_artifacts,
            recent_observations=observations,
            latest_execution=latest_execution,
        )

    def _log(self, message: str) -> None:
        if getattr(self, "verbose", True):
            print(message, flush=True)

    def _notify_tree_update(self, tree_snapshot: AttackTreeSnapshot) -> None:
        if self._on_tree_update:
            self._on_tree_update(tree_snapshot)

    def _timed_agent_run(self, agent_name: str, zone: str, payload: object) -> tuple[object, float]:
        started = time.monotonic()
        output = self.agents[agent_name].run(zone, payload)
        return output, time.monotonic() - started


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


def _create_observation(
    round_num: int,
    source: str,
    result: ExecutionResult | None,
    agent_output: ReconOutput | StrategyOutput,
) -> Observation:
    """从 ExecutionResult（可为None）和 Agent 输出创建 Observation。"""
    return Observation(
        round=round_num,
        source=source,
        command=result.command if result else "",
        result_type=agent_output.result_type,
        summary=agent_output.summary,
        key_findings=agent_output.key_findings,
        next_action_hint=agent_output.next_action_hint,
    )


def _facts_from_execution(result: ExecutionResult) -> GlobalFacts:
    credentials = re.findall(r"(?:password|token|secret|key)[:=]\s*(\S+)", "\n".join(result.findings), re.I)
    artifacts = [artifact.path for artifact in result.artifacts]
    return GlobalFacts(
        flags=result.flag_candidates,
        credentials=credentials,
        artifacts=artifacts,
    )


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
