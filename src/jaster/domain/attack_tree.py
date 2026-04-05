from __future__ import annotations

import hashlib
from collections import defaultdict

from .models import (
    AttackTreeSnapshot,
    GlobalFacts,
    NodeStatus,
    TreeNodeSnapshot,
    TreePatch,
)


def _stable_key(parent_key: str, kind: str, locator: str, title: str) -> str:
    material = "||".join([parent_key, kind, locator.strip().lower(), title.strip().lower()])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


class AttackTree:
    def __init__(self, snapshot: AttackTreeSnapshot | None = None) -> None:
        if snapshot is None:
            snapshot = AttackTreeSnapshot()
        self._nodes = {node.key: node for node in snapshot.nodes}
        self._selected_node_key = snapshot.selected_node_key
        self._facts = snapshot.facts

    @classmethod
    def bootstrap(cls, target: str, *, title: str | None = None) -> "AttackTree":
        root = TreeNodeSnapshot(
            key=_stable_key("", "target", target, title or target),
            title=title or target,
            kind="target",
            status=NodeStatus.exploring,
            priority=100,
            reason="Run bootstrap",
        )
        return cls(AttackTreeSnapshot(nodes=[root], selected_node_key=root.key))

    def snapshot(self) -> AttackTreeSnapshot:
        children = defaultdict(list)
        for node in self._nodes.values():
            children[node.parent_key].append(node.key)
        frontier = [
            node.key
            for node in self._nodes.values()
            if node.status in {NodeStatus.unexplored, NodeStatus.exploring}
            and not children.get(node.key)
        ]
        frontier.sort(key=lambda key: (-self._nodes[key].priority, self._nodes[key].title, key))
        path = self.path_keys(self._selected_node_key)
        nodes = sorted(
            self._nodes.values(),
            key=lambda node: (self.depth(node.key), -node.priority, node.title, node.key),
        )
        return AttackTreeSnapshot(
            selected_node_key=self._selected_node_key,
            selected_path_keys=path,
            frontier_keys=frontier,
            nodes=nodes,
            facts=self._facts,
        )

    def depth(self, key: str) -> int:
        depth = 0
        node = self._nodes.get(key)
        while node is not None and node.parent_key:
            depth += 1
            node = self._nodes.get(node.parent_key)
        return depth

    def path_keys(self, key: str) -> list[str]:
        if not key or key not in self._nodes:
            return []
        path = []
        node = self._nodes[key]
        while True:
            path.append(node.key)
            if not node.parent_key:
                break
            node = self._nodes[node.parent_key]
        path.reverse()
        return path

    def apply_patch(self, patch: TreePatch) -> AttackTreeSnapshot:
        for node_patch in patch.add_nodes:
            key = _stable_key(
                node_patch.parent_key,
                node_patch.kind.value,
                node_patch.locator,
                node_patch.title,
            )
            if key in self._nodes:
                node = self._nodes[key]
                node.priority = max(node.priority, node_patch.priority)
                if node_patch.reason:
                    node.reason = node_patch.reason
                if node_patch.status:
                    node.status = node_patch.status
                continue
            self._nodes[key] = TreeNodeSnapshot(
                key=key,
                parent_key=node_patch.parent_key,
                title=node_patch.title,
                kind=node_patch.kind,
                priority=node_patch.priority,
                reason=node_patch.reason,
                status=node_patch.status,
            )
        for update in patch.update_nodes:
            node = self._nodes.get(update.key)
            if node is None:
                continue
            if update.status is not None:
                node.status = update.status
            if update.priority is not None:
                node.priority = update.priority
            if update.reason is not None:
                node.reason = update.reason
        if patch.selected_node_key is not None:
            self._selected_node_key = patch.selected_node_key
        return self.snapshot()

    def merge_facts(self, facts: GlobalFacts) -> None:
        self._facts.flags = _merge_unique(self._facts.flags, facts.flags)
        self._facts.credentials = _merge_unique(self._facts.credentials, facts.credentials)
        self._facts.services = _merge_unique(self._facts.services, facts.services)
        self._facts.artifacts = _merge_unique(self._facts.artifacts, facts.artifacts)

    def set_selected_node(self, key: str) -> None:
        if key in self._nodes:
            self._selected_node_key = key


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen: list[str] = []
    for item in [*left, *right]:
        if item and item not in seen:
            seen.append(item)
    return seen

