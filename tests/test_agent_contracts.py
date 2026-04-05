from jaster.domain import (
    ActionPlan,
    AttackTree,
    BuilderInput,
    ReconInput,
    StrategyInput,
    SubmissionInput,
)


def test_builder_input_only_has_task() -> None:
    payload = BuilderInput(task="Write a script")
    assert payload.model_dump() == {"task": "Write a script"}


def test_tree_snapshot_is_shared_contract_shape() -> None:
    snapshot = AttackTree.bootstrap("http://target").snapshot()
    shared = ReconInput(objective="recon", tree=snapshot).tree.model_dump()
    strategy = StrategyInput(objective="strategy", tree=snapshot).tree.model_dump()
    assert shared.keys() == strategy.keys()
    assert {"selected_node_key", "selected_path_keys", "frontier_keys", "nodes", "edges", "facts"} <= set(shared.keys())
    assert {"key", "title", "value", "reason", "how"} <= set(shared["nodes"][0].keys())


def test_action_plan_replaces_boolean_routing() -> None:
    plan = ActionPlan(kind="skill", goal="scan ports", expected_result="open ports", skill_name="port_scan", skill_args={"target": "1.1.1.1"})
    dumped = plan.model_dump()
    assert dumped["kind"] == "skill"
    assert "skill_name" in dumped
    assert "builder_task" in dumped


def test_submission_input_is_minimal() -> None:
    payload = SubmissionInput(candidates=["flag{demo}"])
    assert payload.candidates == ["flag{demo}"]
    assert payload.submitted_flags == []

