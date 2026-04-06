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
        return cls(AttackTreeSnapshot(nodes=[root]))

    def snapshot(self) -> AttackTreeSnapshot:
        children = defaultdict(list)
        for node in self._nodes.values():
            children[node.parent_key].append(node.key)
        nodes = sorted(
            self._nodes.values(),
            key=lambda node: (self.depth(node.key), -node.priority, node.title, node.key),
        )
        return AttackTreeSnapshot(
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

    def _collect_descendants(self, key: str) -> list[str]:
        """递归收集所有后代节点的 key"""
        descendants = []
        for node in self._nodes.values():
            if node.parent_key == key:
                descendants.append(node.key)
                descendants.extend(self._collect_descendants(node.key))
        return descendants

    def apply_patch(self, patch: TreePatch) -> AttackTreeSnapshot:
        # 记录新增节点的 key 及其关联的 refs，用于后续双向关联
        new_node_refs: dict[str, list[str]] = {}

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
                # 合并 shared_refs（已有节点也要更新关联）
                if node_patch.shared_refs:
                    for ref_key in node_patch.shared_refs:
                        if ref_key and ref_key not in node.shared_refs:
                            node.shared_refs.append(ref_key)
                continue
            self._nodes[key] = TreeNodeSnapshot(
                key=key,
                parent_key=node_patch.parent_key,
                title=node_patch.title,
                kind=node_patch.kind,
                priority=node_patch.priority,
                reason=node_patch.reason,
                status=node_patch.status,
                shared_refs=list(node_patch.shared_refs),
            )
            if node_patch.shared_refs:
                new_node_refs[key] = node_patch.shared_refs

        # 处理双向关联：在被引用的节点中添加当前节点的 key
        for new_key, refs in new_node_refs.items():
            for ref_key in refs:
                if ref_key in self._nodes and new_key not in self._nodes[ref_key].shared_refs:
                    self._nodes[ref_key].shared_refs.append(new_key)

        for update in patch.update_nodes:
            node = self._nodes.get(update.key)
            if node is None:
                continue
            if update.status is not None:
                node.status = update.status
                # 当节点被标记为 failed，删除其所有后代
                if update.status == NodeStatus.failed:
                    to_delete = self._collect_descendants(update.key)
                    for descendant_key in to_delete:
                        del self._nodes[descendant_key]
            if update.priority is not None:
                node.priority = update.priority
            if update.reason is not None:
                node.reason = update.reason
            # 处理 shared_refs 更新（合并而非替换）
            if update.shared_refs is not None:
                for ref_key in update.shared_refs:
                    if ref_key and ref_key in self._nodes and ref_key not in node.shared_refs:
                        node.shared_refs.append(ref_key)
                        # 双向关联
                        if node.key not in self._nodes[ref_key].shared_refs:
                            self._nodes[ref_key].shared_refs.append(node.key)
        return self.snapshot()

    def merge_facts(self, facts: GlobalFacts) -> None:
        self._facts.flags = _merge_unique(self._facts.flags, facts.flags)
        self._facts.credentials = _merge_unique(self._facts.credentials, facts.credentials)
        self._facts.services = _merge_unique(self._facts.services, facts.services)
        self._facts.artifacts = _merge_unique(self._facts.artifacts, facts.artifacts)


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen: list[str] = []
    for item in [*left, *right]:
        if item and item not in seen:
            seen.append(item)
    return seen

