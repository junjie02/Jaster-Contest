from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from .models import (
    TaskNodePatch,
    TaskNodeSnapshot,
    TaskNodeUpdatePatch,
    TaskStatus,
    TaskTreePatch,
    TaskTreeSnapshot,
)

logger = logging.getLogger(__name__)


def _stable_key(parent_key: str, title: str, completion_criteria: str) -> str:
    material = "||".join([parent_key.strip(), title.strip().lower(), completion_criteria.strip().lower()])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


class TaskTree:
    def __init__(self, snapshot: TaskTreeSnapshot | None = None) -> None:
        if snapshot is None:
            snapshot = TaskTreeSnapshot()
        self._nodes = {node.key: node for node in snapshot.nodes}

    @classmethod
    def bootstrap(cls, target: str, *, title: str | None = None) -> "TaskTree":
        root_title = title or f"Exploit {target}"
        root = TaskNodeSnapshot(
            key=_stable_key("", root_title, "Obtain flag or decisive exploitation path"),
            title=root_title,
            reason="Bootstrap root task created from run target.",
            completion_criteria="Obtain flag or decisive exploitation path",
            status=TaskStatus.in_progress,
        )
        return cls(TaskTreeSnapshot(nodes=[root]))

    def snapshot(self) -> TaskTreeSnapshot:
        children = defaultdict(list)
        for node in self._nodes.values():
            children[node.parent_key].append(node.key)
        nodes = sorted(
            self._nodes.values(),
            key=lambda node: (self.depth(node.key), node.title.lower(), node.key),
        )
        return TaskTreeSnapshot(nodes=nodes)

    def depth(self, key: str) -> int:
        depth = 0
        node = self._nodes.get(key)
        while node is not None and node.parent_key:
            depth += 1
            node = self._nodes.get(node.parent_key)
        return depth

    def get(self, key: str) -> TaskNodeSnapshot | None:
        return self._nodes.get(key)

    def nodes_by_key(self) -> dict[str, TaskNodeSnapshot]:
        return dict(self._nodes)

    def apply_patch(self, patch: TaskTreePatch) -> TaskTreeSnapshot:
        for add in patch.add_nodes:
            if not add.parent_key:
                logger.warning("add_node skipped: parent_key is empty (title=%r)", add.title)
                continue
            if add.parent_key not in self._nodes:
                logger.warning(
                    "add_node skipped: parent_key %r not found in tree (title=%r, existing_keys=%s)",
                    add.parent_key, add.title, sorted(self._nodes.keys())[:10],
                )
                continue
            key = _stable_key(add.parent_key, add.title, add.completion_criteria)
            if key in self._nodes:
                existing = self._nodes[key]
                self._merge_existing(existing, add)
                continue
            self._nodes[key] = TaskNodeSnapshot(
                key=key,
                parent_key=add.parent_key,
                title=add.title,
                reason=add.reason,
                completion_criteria=add.completion_criteria,
                status=add.status,
                latest_summary=add.latest_summary,
                latest_findings=list(add.latest_findings),
                attempt_count=add.attempt_count,
            )

        for update in patch.update_nodes:
            node = self._nodes.get(update.key)
            if node is None:
                continue
            self._apply_update(node, update)
        return self.snapshot()

    @staticmethod
    def _merge_existing(node: TaskNodeSnapshot, add: TaskNodePatch) -> None:
        if add.reason:
            node.reason = add.reason
        if add.completion_criteria:
            node.completion_criteria = add.completion_criteria
        if add.status:
            node.status = add.status
        if add.latest_summary:
            node.latest_summary = add.latest_summary
        if add.latest_findings:
            merged = list(node.latest_findings)
            for item in add.latest_findings:
                if item and item not in merged:
                    merged.append(item)
            node.latest_findings = merged
        node.attempt_count = max(node.attempt_count, add.attempt_count)

    @staticmethod
    def _apply_update(node: TaskNodeSnapshot, update: TaskNodeUpdatePatch) -> None:
        if update.title is not None:
            node.title = update.title
        if update.reason is not None:
            node.reason = update.reason
        if update.completion_criteria is not None:
            node.completion_criteria = update.completion_criteria
        if update.status is not None:
            node.status = update.status
        if update.latest_summary is not None:
            node.latest_summary = update.latest_summary
        if update.latest_findings is not None:
            node.latest_findings = list(update.latest_findings)
        if update.attempt_count is not None:
            node.attempt_count = update.attempt_count
