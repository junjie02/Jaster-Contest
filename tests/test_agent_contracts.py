from jaster.domain import ActionPlan, AttackTree, BuilderInput, ReconInput, StrategyInput, SubmissionInput
from jaster.runtime.orchestrator import _resolve_node_context


def test_builder_input_only_has_task() -> None:
    payload = BuilderInput(task="Write a script")
    assert payload.model_dump() == {"task": "Write a script"}


def test_tree_snapshot_is_shared_contract_shape() -> None:
    snapshot = AttackTree.bootstrap("http://target").snapshot()
    shared = ReconInput(objective="recon", tree=snapshot).tree.model_dump()
    context = _resolve_node_context(AttackTree(snapshot), snapshot.nodes[0].key)
    strategy = StrategyInput(objective="strategy", target_node=context.target_node).target_node.model_dump()

    assert {"nodes", "facts"} <= set(shared.keys())
    assert {"key", "title", "locator", "value", "reason", "how", "evidence"} <= set(shared["nodes"][0].keys())
    assert {"key", "title", "locator", "value", "reason", "how", "evidence"} <= set(strategy.keys())


def test_action_plan_replaces_boolean_routing() -> None:
    plan = ActionPlan(
        kind="function",
        goal="scan ports",
        expected_result="open ports",
        function_name="port_scan",
        function_args={"target": "1.1.1.1"},
        executor_brief="目标: 1.1.1.1",
    )
    dumped = plan.model_dump()
    assert dumped["kind"] == "function"
    assert dumped["function_name"] == "port_scan"
    assert dumped["executor_brief"] == "目标: 1.1.1.1"


def test_submission_input_is_minimal() -> None:
    payload = SubmissionInput(candidates=["flag{demo}"])
    assert payload.candidates == ["flag{demo}"]
    assert payload.submitted_flags == []
