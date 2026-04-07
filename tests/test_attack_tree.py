from jaster.domain import AttackTree, NodeKind, NodeStatus, TreePatch
from jaster.domain.models import NodePatch, NodeUpdatePatch


def test_attack_tree_snapshot_contains_rich_fields() -> None:
    tree = AttackTree.bootstrap("http://target")
    snapshot = tree.snapshot()
    root = snapshot.nodes[0]

    assert root.key
    assert root.title == "http://target"
    assert root.locator == "http://target"
    assert root.value
    assert root.reason
    assert root.how
    assert root.evidence == []
    assert root.key_findings == []


def test_attack_tree_apply_patch_persists_and_merges_rich_node_fields() -> None:
    tree = AttackTree.bootstrap("http://target")
    root_key = tree.snapshot().nodes[0].key

    snapshot = tree.apply_patch(
        TreePatch(
            add_nodes=[
                NodePatch(
                    parent_key=root_key,
                    title="login page",
                    kind=NodeKind.entry,
                    locator="http://target/login",
                    priority=90,
                    value="Potential auth surface",
                    reason="Found during crawl",
                    how="Test auth and reset flows",
                    evidence=["GET /login"],
                    shared_refs=[],
                    key_findings=["username,password"],
                )
            ]
        )
    )

    child = next(node for node in snapshot.nodes if node.parent_key == root_key)
    assert child.locator == "http://target/login"
    assert child.value == "Potential auth surface"
    assert child.how == "Test auth and reset flows"
    assert child.evidence == ["GET /login"]
    assert child.key_findings == ["username,password"]

    updated = tree.apply_patch(
        TreePatch(
            add_nodes=[
                NodePatch(
                    parent_key=root_key,
                    title="login page",
                    kind=NodeKind.entry,
                    locator="http://target/login",
                    priority=95,
                    value="Confirmed auth surface",
                    reason="Response pattern confirmed",
                    how="Probe login flow and session handling",
                    evidence=["POST /login 302"],
                    shared_refs=[],
                    key_findings=["connect.sid"],
                )
            ],
            update_nodes=[
                NodeUpdatePatch(
                    key=child.key,
                    status=NodeStatus.success,
                    value="Valid auth endpoint with controllable response",
                    how="Use for session validation",
                    evidence=["Set-Cookie: connect.sid=..."],
                    key_findings=["302 -> /api/profile"],
                )
            ],
        )
    )

    selected = next(node for node in updated.nodes if node.key == child.key)
    assert selected.status == NodeStatus.success
    assert selected.priority == 95
    assert selected.value == "Valid auth endpoint with controllable response"
    assert selected.how == "Use for session validation"
    assert selected.evidence == [
        "GET /login",
        "POST /login 302",
        "Set-Cookie: connect.sid=...",
    ]
    assert selected.key_findings == [
        "username,password",
        "connect.sid",
        "302 -> /api/profile",
    ]
