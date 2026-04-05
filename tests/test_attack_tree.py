from jaster.domain import AttackTree, EdgeRelation, NodeKind, NodeStatus, TreePatch
from jaster.domain.models import EdgePatch, NodePatch, NodeUpdatePatch


def test_attack_tree_snapshot_contains_required_fields() -> None:
    tree = AttackTree.bootstrap("http://target")
    snapshot = tree.snapshot()
    root = snapshot.nodes[0]
    assert root.key
    assert root.title == "http://target"
    assert root.value
    assert root.reason
    assert root.how


def test_attack_tree_apply_patch_adds_and_updates_nodes() -> None:
    tree = AttackTree.bootstrap("http://target")
    root_key = tree.snapshot().nodes[0].key
    patch = TreePatch(
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
            )
        ]
    )
    snapshot = tree.apply_patch(patch)
    assert len(snapshot.nodes) == 2
    child = next(node for node in snapshot.nodes if node.parent_key == root_key)
    tree.apply_patch(
        TreePatch(
            update_nodes=[
                NodeUpdatePatch(
                    key=child.key,
                    status=NodeStatus.success,
                    value="Valid auth endpoint with controllable response",
                )
            ],
            add_edges=[
                EdgePatch(
                    from_key=root_key,
                    to_key=child.key,
                    relation=EdgeRelation.evidence,
                    reason="Confirmed during recon",
                    how="Use login endpoint as branch root",
                )
            ],
            selected_node_key=child.key,
        )
    )
    updated = tree.snapshot()
    assert updated.selected_node_key == child.key
    selected = next(node for node in updated.nodes if node.key == child.key)
    assert selected.status == NodeStatus.success
    assert updated.edges[0].relation == EdgeRelation.evidence

