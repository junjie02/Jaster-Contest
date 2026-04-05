from pathlib import Path

from jaster.domain import (
    ActionPlan,
    ChallengeSpec,
    ReconOutput,
    ReflectionOutput,
    StrategyOutput,
    SubmissionOutput,
    TreePatch,
)
from jaster.domain.models import BuilderOutput
from jaster.runtime.orchestrator import JasterOrchestrator
from jaster.storage.files import FileRunStore


class FakeAgent:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def run(self, zone, payload):
        return self.outputs.pop(0)


class FakeSkillExecutor:
    def run(self, skill_name, skill_args, *, cwd):
        from jaster.domain import ExecutionResult

        cwd.mkdir(parents=True, exist_ok=True)
        return ExecutionResult(
            success=True,
            summary=f"{skill_name} ok",
            findings=["found login page"],
            flag_candidates=[],
            command=f"{skill_name} {skill_args}",
        )


class FakeBuilderExecutor:
    def run(self, builder_output, **kwargs):
        from jaster.domain import ExecutionResult

        return ExecutionResult(
            success=True,
            summary=builder_output.summary,
            findings=["builder parsed source and found flag"],
            flag_candidates=["flag{demo}"],
        )


def test_orchestrator_runs_end_to_end(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "runs")
    orchestrator = JasterOrchestrator.__new__(JasterOrchestrator)
    orchestrator.store = store
    orchestrator.prompt_root = tmp_path
    orchestrator.skill_catalog = type("FakeCatalog", (), {"list_available": lambda self: []})()
    orchestrator.skill_executor = FakeSkillExecutor()
    orchestrator.builder_executor = FakeBuilderExecutor()
    orchestrator.agents = {
        "recon": FakeAgent(
            [
                ReconOutput(
                    summary="Recon found a login page",
                    done=True,
                    action=ActionPlan(kind="finish", goal="recon done"),
                    tree_patch=TreePatch(),
                )
            ]
        ),
        "strategy": FakeAgent(
            [
                StrategyOutput(
                    summary="Use builder on selected branch",
                    selected_node_key="",
                    action=ActionPlan(
                        kind="builder",
                        goal="Parse source dump",
                        expected_result="find sensitive data",
                        builder_task="Read the source dump and extract any flag-like value.",
                    ),
                    goal_reached=False,
                    tree_patch=TreePatch(),
                )
            ]
        ),
        "builder": FakeAgent(
            [
                BuilderOutput(
                    summary="builder script",
                    script='import json,sys; data=json.load(sys.stdin); print(json.dumps({"summary":"ok","findings":["x"],"artifacts":[],"flag_candidates":["flag{demo}"]}))',
                )
            ]
        ),
        "reflection": FakeAgent(
            [
                ReflectionOutput(
                    summary="Reflection confirms success",
                    next_focus_key="",
                    halt=True,
                    flag_candidates=["flag{demo}"],
                    tree_patch=TreePatch(),
                )
            ]
        ),
        "submission": FakeAgent(
            [
                SubmissionOutput(
                    should_submit=True,
                    flag="flag{demo}",
                    reason="Directly observed in builder output",
                )
            ]
        ),
    }
    challenge = ChallengeSpec(target="http://target", zone="zone1")
    state = orchestrator.run(challenge, max_recon_steps=1, max_rounds=2)
    assert state.submitted_flags == ["flag{demo}"]
    assert state.rounds_completed == 1
    assert store.run_dir(state.run_id).exists()

