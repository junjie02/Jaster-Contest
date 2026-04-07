from pathlib import Path
import json
from types import SimpleNamespace

from jaster.domain import (
    ActionPlan,
    ArtifactRef,
    ChallengeSpec,
    ExecutionResult,
    Observation,
    ReconOutput,
    ReflectionOutput,
    StrategyOutput,
    SubmissionOutput,
    TreePatch,
)
from jaster.domain.attack_tree import AttackTree
from jaster.domain.models import BuilderOutput
from jaster.runtime.orchestrator import JasterOrchestrator, _compact_observations
from jaster.storage.files import FileRunStore


class FakeAgent:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.last_trace = None

    def run(self, zone, payload):
        if not self.outputs:
            raise AssertionError("Agent received more calls than expected")
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


def _make_orchestrator(tmp_path: Path) -> JasterOrchestrator:
    store = FileRunStore(tmp_path / "runs")
    orchestrator = JasterOrchestrator.__new__(JasterOrchestrator)
    orchestrator.store = store
    orchestrator.prompt_root = tmp_path
    orchestrator.skill_catalog = type("FakeCatalog", (), {"list_available": lambda self: []})()
    orchestrator.skill_executor = FakeSkillExecutor()
    orchestrator.builder_executor = FakeBuilderExecutor()
    orchestrator.verbose = False
    orchestrator._last_builder_trace = None
    orchestrator._on_tree_update = None
    return orchestrator


def test_orchestrator_runs_end_to_end(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    bootstrap_tree = AttackTree.bootstrap("http://target").snapshot()
    target_key = bootstrap_tree.nodes[0].key
    exploitable_key = "node-1"

    orchestrator.agents = {
        "recon": FakeAgent(
            [
                ReconOutput(
                    summary="Recon found an exploitable branch",
                    discover_vulnerability=True,
                    selected_node_key=target_key,
                    action=ActionPlan(kind="finish", goal="recon done"),
                    tree_patch=TreePatch(
                        add_nodes=[
                            {
                                "parent_key": target_key,
                                "title": "LFI branch",
                                "kind": "weakness",
                                "locator": "/?page=",
                                "priority": 95,
                                "value": "confirmed traversal",
                                "reason": "high signal",
                                "how": "read files",
                                "evidence": ["../../../etc/passwd"],
                                "status": "success",
                                "shared_refs": [],
                            }
                        ]
                    ),
                )
            ]
        ),
        "strategy": FakeAgent(
            [
                StrategyOutput(
                    summary="Use builder on selected branch",
                    selected_node_key=exploitable_key,
                    action=ActionPlan(
                        kind="builder",
                        goal="Parse source dump",
                        expected_result="find sensitive data",
                        builder_task="Read the source dump and extract any flag-like value.",
                    ),
                    goal_reached=True,
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
                    next_focus_key=exploitable_key,
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
    state = orchestrator.run(challenge, max_rounds=3)

    assert state.submitted_flags == ["flag{demo}"]
    assert state.rounds_completed == 3
    assert (orchestrator.store.run_dir(state.run_id) / "rounds" / "001.json").exists()
    assert (orchestrator.store.run_dir(state.run_id) / "rounds" / "002.json").exists()
    assert (orchestrator.store.run_dir(state.run_id) / "rounds" / "003.json").exists()


def test_orchestrator_round_log_includes_llm_inputs(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    bootstrap_tree = AttackTree.bootstrap("http://target").snapshot()
    target_key = bootstrap_tree.nodes[0].key

    recon = FakeAgent(
        [
            ReconOutput(
                summary="Recon found a login page",
                discover_vulnerability=False,
                selected_node_key=target_key,
                action=ActionPlan(kind="finish", goal="recon done"),
                tree_patch=TreePatch(),
            )
        ]
    )
    recon.last_trace = {
        "role": "recon",
        "zone": "zone1",
        "system": "sys",
        "payload": {"objective": "recon"},
        "payload_json": "{\"objective\":\"recon\"}",
        "prompt": "prompt",
    }
    orchestrator.agents = {"recon": recon}

    challenge = ChallengeSpec(target="http://target", zone="zone1")
    state = orchestrator.run(challenge, max_rounds=1)

    round_payload = json.loads((orchestrator.store.run_dir(state.run_id) / "rounds" / "001.json").read_text(encoding="utf-8"))
    assert round_payload["agent"] == "recon"
    assert round_payload["phase"] == "recon_round_1"
    assert round_payload["recon_input"]["prompt"] == "prompt"
    assert round_payload["recon_input"]["payload"]["objective"] == "recon"
    assert state.rounds_completed == 1


def test_orchestrator_uses_shared_round_budget_and_keeps_chronological_logs(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    bootstrap_tree = AttackTree.bootstrap("http://target").snapshot()
    target_key = bootstrap_tree.nodes[0].key
    branch_key = "branch-1"

    orchestrator.agents = {
        "recon": FakeAgent(
            [
                ReconOutput(
                    summary="Recon found an exploitable branch",
                    discover_vulnerability=True,
                    selected_node_key=target_key,
                    action=ActionPlan(kind="finish", goal="recon done"),
                    tree_patch=TreePatch(
                        add_nodes=[
                            {
                                "parent_key": target_key,
                                "title": "Confirmed branch",
                                "kind": "weakness",
                                "locator": "/?page=",
                                "priority": 95,
                                "value": "confirmed traversal",
                                "reason": "high signal",
                                "how": "read files",
                                "evidence": ["../../../etc/passwd"],
                                "status": "success",
                                "shared_refs": [],
                            }
                        ]
                    ),
                ),
                ReconOutput(
                    summary="Recon again after strategy",
                    selected_node_key=branch_key,
                    action=ActionPlan(kind="finish", goal="recon follow-up"),
                    tree_patch=TreePatch(),
                ),
            ]
        ),
        "reflection": FakeAgent(
            [
                ReflectionOutput(summary="first reflection", next_focus_key=branch_key, tree_patch=TreePatch()),
                ReflectionOutput(summary="second reflection", next_focus_key=branch_key, tree_patch=TreePatch()),
            ]
        ),
        "strategy": FakeAgent(
            [
                StrategyOutput(
                    summary="Need more recon",
                    selected_node_key=branch_key,
                    action=ActionPlan(kind="finish", goal="pause"),
                    need_recon=True,
                    tree_patch=TreePatch(),
                )
            ]
        ),
    }
    challenge = ChallengeSpec(target="http://target", zone="zone1")
    state = orchestrator.run(challenge, max_rounds=4)

    rounds_dir = orchestrator.store.run_dir(state.run_id) / "rounds"
    files = sorted(path.name for path in rounds_dir.iterdir())
    assert files == ["001.json", "002.json", "003.json", "004.json"]

    payloads = [
        json.loads((rounds_dir / name).read_text(encoding="utf-8"))
        for name in files
    ]
    assert [item["agent"] for item in payloads] == ["recon", "reflection", "strategy", "recon"]
    assert [item["phase"] for item in payloads] == [
        "recon_round_1",
        "reflection_round_2",
        "strategy_round_3",
        "recon_round_4",
    ]
    assert state.rounds_completed == 4


def test_orchestrator_compacts_prompt_payload_and_records_current_execution(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    target_key = AttackTree.bootstrap("http://target").snapshot().nodes[0].key

    class CapturingReconAgent(FakeAgent):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.payloads = []

        def run(self, zone, payload):
            self.payloads.append(payload)
            return super().run(zone, payload)

    recon = CapturingReconAgent(
        [
            ReconOutput(
                summary="Recon found useful data",
                discover_vulnerability=False,
                selected_node_key=target_key,
                result_type="ok",
                next_action_hint="continue probing",
                action=ActionPlan(kind="finish", goal="recon done"),
                tree_patch=TreePatch(),
            )
        ]
    )
    orchestrator.agents = {"recon": recon}

    long_command = "curl " + ("a" * 220)
    long_stdout = "b" * 800

    def fake_execute_action(**_: object) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            summary="ok",
            findings=[f"finding-{idx}" for idx in range(10)],
            artifacts=[ArtifactRef(kind="file", path=f"/tmp/{idx}.txt") for idx in range(5)],
            stdout=long_stdout,
            exit_code=0,
            command=long_command,
        )

    orchestrator._execute_action = fake_execute_action  # type: ignore[assignment]

    challenge = ChallengeSpec(target="http://target", zone="zone1")
    state = orchestrator.run(challenge, max_rounds=1)

    assert state.observations[0].command == long_command
    payload = recon.payloads[0]
    assert payload.tree.facts.artifacts == []
    assert payload.recent_observations == []


def test_compact_observations_keeps_last_fifty_items() -> None:
    observations = [
        {
            "round": idx,
            "source": "recon",
            "command": f"cmd-{idx}",
            "result_type": "ok",
            "summary": f"summary-{idx}",
            "next_action_hint": f"hint-{idx}",
        }
        for idx in range(60)
    ]

    compacted = _compact_observations([Observation.model_validate(item) for item in observations])

    assert len(compacted) == 50
    assert compacted[0].round == 10
    assert compacted[-1].round == 59


def test_initial_http_curl_keeps_full_response_body(monkeypatch, tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    target_key = AttackTree.bootstrap("http://target").snapshot().nodes[0].key

    class CapturingReconAgent(FakeAgent):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.payloads = []

        def run(self, zone, payload):
            self.payloads.append(payload)
            return super().run(zone, payload)

    recon = CapturingReconAgent(
        [
            ReconOutput(
                summary="done",
                discover_vulnerability=False,
                selected_node_key=target_key,
                action=ActionPlan(kind="finish", goal="stop"),
                tree_patch=TreePatch(),
            )
        ]
    )
    orchestrator.agents = {"recon": recon}
    body = "A" * 9000

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=body, stderr=""),
    )

    challenge = ChallengeSpec(target="http://target", zone="zone1")
    orchestrator.run(challenge, max_rounds=1)

    latest_execution = recon.payloads[0].latest_execution
    assert latest_execution is not None
    assert latest_execution.summary == "Initial HTTP response captured"
    assert latest_execution.findings == []
    assert latest_execution.stdout == body
