from __future__ import annotations

import re
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
    ReflectionInput,
    RunState,
    StrategyInput,
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
    ) -> None:
        self.store = store
        self.prompt_root = prompt_root
        self.skill_catalog = SkillCatalog(skills_dir)
        self.skill_executor = SkillExecutor(self.skill_catalog)
        self.builder_executor = BuilderExecutor()
        self.agents = build_agents(prompt_root, llm)

    def run(self, challenge: ChallengeSpec, *, max_recon_steps: int = 3, max_rounds: int = 12) -> RunState:
        run_id = self.store.new_run_id()
        tree = AttackTree.bootstrap(challenge.target)
        state = RunState(run_id=run_id, challenge=challenge, tree=tree.snapshot())
        self.store.create(state)

        latest_execution: ExecutionResult | None = None
        last_reflection = ""

        for recon_index in range(1, max_recon_steps + 1):
            recon_out = self.agents["recon"].run(
                challenge.zone,
                ReconInput(
                    objective=f"Recon the target {challenge.target} and expand the global attack tree.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-6:],
                    latest_execution=latest_execution,
                    available_skills=self.skill_catalog.list_available(),
                ),
            )
            tree.apply_patch(recon_out.tree_patch)
            latest_execution = self._execute_action(
                run_id=run_id,
                challenge=challenge,
                action=recon_out.action,
                observations=state.observations[-6:],
                latest_execution=latest_execution,
            )
            state.observations.append(_execution_to_observation("recon", latest_execution))
            tree.merge_facts(_facts_from_execution(latest_execution))
            state.tree = tree.snapshot()
            self.store.append_round(
                run_id,
                recon_index,
                {"phase": "recon", "recon": recon_out.model_dump(), "execution": latest_execution.model_dump()},
            )
            if recon_out.done or recon_out.action.kind == "finish":
                break

        for round_index in range(1, max_rounds + 1):
            strategy_out = self.agents["strategy"].run(
                challenge.zone,
                StrategyInput(
                    objective=f"Exploit the target {challenge.target} and capture the flag.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-8:],
                    latest_execution=latest_execution,
                    last_reflection=last_reflection,
                ),
            )
            selected_key = strategy_out.selected_node_key or (state.tree.frontier_keys[0] if state.tree.frontier_keys else "")
            if selected_key:
                tree.set_selected_node(selected_key)
            strategy_out.tree_patch.selected_node_key = selected_key or strategy_out.tree_patch.selected_node_key
            tree.apply_patch(strategy_out.tree_patch)
            latest_execution = self._execute_action(
                run_id=run_id,
                challenge=challenge,
                action=strategy_out.action,
                observations=state.observations[-8:],
                latest_execution=latest_execution,
            )
            state.observations.append(_execution_to_observation("strategy", latest_execution))
            tree.merge_facts(_facts_from_execution(latest_execution))

            reflection_out = self.agents["reflection"].run(
                challenge.zone,
                ReflectionInput(
                    objective="Reflect on the latest action, correct drift, and update the global attack tree.",
                    tree=tree.snapshot(),
                    recent_observations=state.observations[-8:],
                    latest_execution=latest_execution,
                    last_strategy=strategy_out.summary,
                ),
            )
            last_reflection = reflection_out.summary
            tree.apply_patch(reflection_out.tree_patch)
            if reflection_out.next_focus_key:
                tree.set_selected_node(reflection_out.next_focus_key)

            candidates = _merge_flag_candidates(strategy_out.flag_candidates, latest_execution.flag_candidates, reflection_out.flag_candidates)
            submission_out = None
            if candidates:
                submission_out = self.agents["submission"].run(
                    challenge.zone,
                    SubmissionInput(
                        candidates=candidates,
                        recent_observations=state.observations[-5:],
                        submitted_flags=state.submitted_flags,
                    ),
                )
                if submission_out.should_submit and submission_out.flag and submission_out.flag not in state.submitted_flags:
                    state.submitted_flags.append(submission_out.flag)
                    tree.merge_facts(GlobalFacts(flags=[submission_out.flag]))

            state.rounds_completed += 1
            state.tree = tree.snapshot()
            self.store.append_round(
                run_id,
                max_recon_steps + round_index,
                {
                    "phase": "main",
                    "strategy": strategy_out.model_dump(),
                    "execution": latest_execution.model_dump(),
                    "reflection": reflection_out.model_dump(),
                    "submission": submission_out.model_dump() if submission_out else None,
                },
            )
            self.store.save_state(state)
            if strategy_out.goal_reached or reflection_out.halt:
                break
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
        work_dir = run_dir / "artifacts" / f"step-{len(list((run_dir / 'rounds').glob('*.json'))) + 1:03d}"
        if action.kind == "finish":
            return ExecutionResult(success=True, summary=action.goal)
        if action.kind == "skill":
            return self.skill_executor.run(action.skill_name or "", action.skill_args, cwd=work_dir)
        builder_output = self.agents["builder"].run(
            challenge.zone,
            BuilderInput(task=action.builder_task or action.goal),
        )
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


def _execution_to_observation(source: str, result: ExecutionResult) -> Observation:
    return Observation(
        source=source,
        summary=result.summary,
        details={
            "success": result.success,
            "findings": result.findings,
            "flag_candidates": result.flag_candidates,
            "stderr": result.stderr,
            "command": result.command,
        },
        artifacts=result.artifacts,
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
