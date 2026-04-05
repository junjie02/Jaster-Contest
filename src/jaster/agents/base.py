from __future__ import annotations

import json
from typing import Generic, TypeVar

from pydantic import BaseModel

from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.prompts import PromptLibrary

InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)

STRICT_JSON_SYSTEM = (
    "You are a strict JSON generator. Follow the role instructions and return exactly one JSON object."
)


class JsonAgent(Generic[InputModel, OutputModel]):
    role: str
    input_model: type[InputModel]
    output_model: type[OutputModel]

    def __init__(self, llm: OpenAIChatClient, prompts: PromptLibrary) -> None:
        self.llm = llm
        self.prompts = prompts
        self.last_trace: dict[str, object] | None = None

    def run(self, zone: str, payload: InputModel) -> OutputModel:
        payload_json = json.dumps(payload.model_dump(), ensure_ascii=False, indent=2)
        prompt = self.prompts.render(
            self.role,
            zone=zone,
            payload_json=payload_json,
        )
        self.last_trace = {
            "role": self.role,
            "zone": zone,
            "system": STRICT_JSON_SYSTEM,
            "payload": payload.model_dump(),
            "payload_json": payload_json,
            "prompt": prompt,
        }
        response = self.llm.complete_json(system=STRICT_JSON_SYSTEM, prompt=prompt)
        self.last_trace["raw_response"] = response
        normalized = _normalize_agent_response(self.role, response)
        self.last_trace["normalized_response"] = normalized
        return self.output_model.model_validate(normalized)


_NODE_KIND_ALIASES = {
    "recon": "entry",
    "service": "asset",
    "surface": "entry",
    "vuln": "weakness",
    "exploit": "technique",
}

_NODE_STATUS_ALIASES = {
    "pending": "unexplored",
    "todo": "unexplored",
    "confirmed": "success",
    "done": "success",
    "active": "exploring",
}


def _normalize_agent_response(role: str, payload: dict) -> dict:
    normalized = dict(payload or {})
    if role in {"recon", "strategy", "reflection"}:
        normalized["tree_patch"] = _normalize_tree_patch(normalized.get("tree_patch") or {}, role=role)
    if role in {"recon", "strategy"}:
        normalized["action"] = _normalize_action(normalized.get("action") or {}, parent=normalized)
    if role == "recon":
        normalized.setdefault("summary", str(normalized.get("recon_summary") or normalized.get("reason") or ""))
        normalized["done"] = bool(normalized.get("done", normalized.get("recon_complete", False)))
    if role == "strategy":
        normalized.setdefault("summary", str(normalized.get("reasoning") or normalized.get("summary") or ""))
        normalized["goal_reached"] = bool(normalized.get("goal_reached", normalized.get("mission_complete", False)))
        normalized["flag_candidates"] = _string_list(
            normalized.get("flag_candidates") or normalized.get("flags_found") or []
        )
    if role == "reflection":
        normalized.setdefault("summary", str(normalized.get("summary") or normalized.get("progress") or ""))
        normalized["next_focus_key"] = str(normalized.get("next_focus_key") or normalized.get("selected_node_key") or "")
        normalized["halt"] = bool(normalized.get("halt", normalized.get("mission_complete", False)))
        normalized["flag_candidates"] = _string_list(
            normalized.get("flag_candidates") or normalized.get("flags_found") or []
        )
    if role == "builder":
        normalized.setdefault("summary", str(normalized.get("summary") or ""))
        if "script" not in normalized:
            normalized["script"] = str(normalized.get("code") or normalized.get("python") or "")
    if role == "submission":
        normalized["flag"] = normalized.get("flag", normalized.get("answer"))
        normalized.setdefault("reason", str(normalized.get("reason") or normalized.get("summary") or ""))
    return normalized


def _normalize_action(action: dict, *, parent: dict) -> dict:
    source = dict(action or {})
    normalized: dict[str, object] = {}
    if "kind" not in source:
        if source.get("skill") or source.get("skill_name") or parent.get("use_skill"):
            normalized["kind"] = "skill"
        elif source.get("builder_task") or parent.get("needs_builder"):
            normalized["kind"] = "builder"
        else:
            action_type = str(parent.get("action_type") or "").strip().lower()
            normalized["kind"] = "finish" if action_type == "finish" else "builder"
    else:
        normalized["kind"] = source.get("kind")
    if "goal" not in normalized:
        normalized["goal"] = str(
            source.get("goal")
            or source.get("reason")
            or parent.get("stage_goal")
            or parent.get("summary")
            or "Continue the current plan."
        )
    normalized["expected_result"] = str(
        source.get("expected_result")
        or source.get("expected_output")
        or parent.get("expected_result")
        or parent.get("expected_output")
        or ""
    )
    normalized["skill_name"] = source.get("skill_name") or source.get("skill")
    normalized["skill_args"] = source.get("skill_args") or source.get("params") or source.get("arguments") or {}
    normalized["builder_task"] = str(
        source.get("builder_task")
        or parent.get("builder_task")
        or parent.get("builder_goal")
        or parent.get("builder_context")
        or normalized["goal"]
    )
    if normalized["kind"] == "finish":
        normalized["skill_name"] = None
        normalized["skill_args"] = {}
        normalized["builder_task"] = None
    if normalized["kind"] == "skill" and not normalized["skill_name"]:
        normalized["kind"] = "builder"
    return normalized


def _normalize_tree_patch(tree_patch: dict, *, role: str) -> dict:
    normalized = dict(tree_patch or {})
    add_nodes = [_normalize_node_patch(item, role=role) for item in normalized.get("add_nodes", []) if isinstance(item, dict)]
    update_nodes = [_normalize_node_update(item) for item in normalized.get("update_nodes", []) if isinstance(item, dict)]
    add_edges = [_normalize_edge_patch(item) for item in normalized.get("add_edges", []) if isinstance(item, dict)]
    return {
        "add_nodes": [item for item in add_nodes if item],
        "update_nodes": [item for item in update_nodes if item],
        "add_edges": [item for item in add_edges if item],
        "selected_node_key": normalized.get("selected_node_key"),
    }


def _normalize_node_patch(node: dict, *, role: str) -> dict:
    kind = str(node.get("kind") or node.get("node_role") or role).strip().lower()
    kind = _NODE_KIND_ALIASES.get(kind, kind)
    status = str(node.get("status") or "").strip().lower()
    status = _NODE_STATUS_ALIASES.get(status, status or "unexplored")
    locator = node.get("locator") or node.get("scope_locator") or node.get("scope") or node.get("target") or ""
    evidence = node.get("evidence") or node.get("evidence_refs") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]
    parent_key = str(node.get("parent_key") or node.get("parent_hint") or "")
    if not parent_key:
        return {}
    return {
        "parent_key": parent_key,
        "title": str(node.get("title") or locator or kind),
        "kind": kind,
        "locator": str(locator),
        "priority": _normalize_priority(node.get("priority") or node.get("priority_weight")),
        "value": str(node.get("value") or node.get("high_value_info") or node.get("reason") or ""),
        "reason": str(node.get("reason") or node.get("why") or ""),
        "how": str(node.get("how") or node.get("exploit_method") or node.get("label") or ""),
        "evidence": _string_list(evidence),
        "status": status,
    }


def _normalize_node_update(node: dict) -> dict:
    status = node.get("status")
    if status is not None:
        status = _NODE_STATUS_ALIASES.get(str(status).strip().lower(), str(status).strip().lower())
    return {
        "key": str(node.get("key") or node.get("node_key") or ""),
        "status": status,
        "priority": _normalize_priority(node.get("priority") or node.get("priority_weight"))
        if node.get("priority") is not None or node.get("priority_weight") is not None
        else None,
        "value": node.get("value"),
        "reason": node.get("reason"),
        "how": node.get("how"),
        "evidence": _string_list(node.get("evidence") or []) if node.get("evidence") is not None else None,
    }


def _normalize_edge_patch(edge: dict) -> dict:
    return {
        "from_key": str(edge.get("from_key") or edge.get("source") or ""),
        "to_key": str(edge.get("to_key") or edge.get("target") or ""),
        "relation": str(edge.get("relation") or edge.get("edge_type") or "dependency"),
        "reason": str(edge.get("reason") or edge.get("why_connected") or edge.get("label") or ""),
        "how": str(edge.get("how") or edge.get("how_to_exploit") or ""),
    }


def _normalize_priority(value: object) -> int:
    mapping = {"high": 90, "medium": 60, "low": 30}
    if isinstance(value, str):
        rendered = value.strip().lower()
        if rendered in mapping:
            return mapping[rendered]
        try:
            return int(rendered)
        except ValueError:
            return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    rendered = str(value).strip()
    return [rendered] if rendered else []
