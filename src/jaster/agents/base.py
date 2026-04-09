from __future__ import annotations

import json
from typing import Generic, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError

from jaster.runtime.llm import LLMError, OpenAIChatClient
from jaster.runtime.prompts import PromptLibrary

InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)

STRICT_JSON_SYSTEM = (
    "你是一个严格的json生成器.遵循角色指令并严格返回json字段.请注意你自身的身份，只做你应该做的事。"
)


class JsonAgent(Generic[InputModel, OutputModel]):
    role: str
    input_model: type[InputModel]
    output_model: type[OutputModel]

    def __init__(self, llm: OpenAIChatClient, prompts: PromptLibrary) -> None:
        self.llm = llm
        self.prompts = prompts
        self.last_trace: dict[str, object] | None = None

    def run(self, zone: str, payload: InputModel, *, retry_context: dict[str, object] | None = None) -> OutputModel:
        base_payload = payload.model_dump()
        attempts: list[dict[str, object]] = []
        current_retry_context = dict(retry_context or {})
        max_attempts = max(1, int(getattr(self.llm, "max_retries", 1) or 1))

        for attempt in range(1, max_attempts + 1):
            rendered_payload = dict(base_payload)
            if current_retry_context:
                rendered_payload["retry_context"] = current_retry_context
            payload_json = json.dumps(rendered_payload, ensure_ascii=False, indent=2)
            prompt = self.prompts.render(
                self.role,
                zone=zone,
                payload_json=payload_json,
            )
            attempt_trace: dict[str, object] = {
                "attempt": attempt,
                "payload": rendered_payload,
                "payload_json": payload_json,
                "prompt": prompt,
            }
            attempts.append(attempt_trace)
            try:
                response = self.llm.complete_json(system=STRICT_JSON_SYSTEM, prompt=prompt)
                attempt_trace["raw_response"] = response
                normalized = _normalize_agent_response(self.role, response)
                attempt_trace["normalized_response"] = normalized
                validated = self.output_model.model_validate(normalized)
                self.last_trace = {
                    "role": self.role,
                    "zone": zone,
                    "system": STRICT_JSON_SYSTEM,
                    "attempts": attempts,
                    "succeeded_attempt": attempt,
                }
                return validated
            except Exception as exc:
                attempt_trace["error_type"] = type(exc).__name__
                attempt_trace["error_message"] = str(exc)
                current_retry_context = _build_retry_context(
                    role=self.role,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=exc,
                    attempt_trace=attempt_trace,
                )
                if attempt >= max_attempts:
                    self.last_trace = {
                        "role": self.role,
                        "zone": zone,
                        "system": STRICT_JSON_SYSTEM,
                        "attempts": attempts,
                        "succeeded_attempt": None,
                    }
                    raise
        raise RuntimeError("unreachable")


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
        parent_key = normalized.get("selected_node_key", "")
        normalized["tree_patch"] = _normalize_tree_patch(
            normalized.get("tree_patch") or {}, role=role, parent_key=parent_key
        )
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
    if role == "skill_router":
        selected = normalized.get("selected_skills") or normalized.get("skills") or normalized.get("skill_names") or []
        if isinstance(selected, str):
            selected = [selected]
        normalized["selected_skills"] = _string_list(selected)[:2]
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
        if source.get("function") or source.get("function_name") or parent.get("use_function"):
            normalized["kind"] = "function"
        elif source.get("builder") or parent.get("use_builder"):
            normalized["kind"] = "builder"
        else:
            action_type = str(parent.get("action_type") or "").strip().lower()
            normalized["kind"] = "finish" if action_type == "finish" else "function"
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
    normalized["function_name"] = source.get("function_name") or source.get("function") or source.get("tool_name")
    normalized["function_args"] = source.get("function_args") or source.get("params") or source.get("arguments") or {}
    normalized["executor_brief"] = str(
        source.get("executor_brief")
        or source.get("builder_task")
        or parent.get("executor_brief")
        or parent.get("builder_task")
        or parent.get("execution_brief")
        or normalized["goal"]
    )
    if normalized["kind"] == "finish":
        normalized["function_name"] = None
        normalized["function_args"] = {}
        normalized["executor_brief"] = ""
    if normalized["kind"] == "builder":
        normalized["function_name"] = None
        normalized["function_args"] = {}
    if normalized["kind"] == "function" and not normalized["function_name"]:
        normalized["kind"] = "finish"
    return normalized


def _normalize_tree_patch(tree_patch: dict, *, role: str, parent_key: str = "") -> dict:
    normalized = dict(tree_patch or {})
    add_nodes = [_normalize_node_patch(item, role=role) for item in normalized.get("add_nodes", []) if isinstance(item, dict)]
    update_nodes = [_normalize_node_update(item) for item in normalized.get("update_nodes", []) if isinstance(item, dict)]
    valid_add_nodes = []
    for node in add_nodes:
        has_content = bool(
            node.get("locator")
            or node.get("value")
            or node.get("reason")
            or node.get("how")
            or node.get("evidence")
        )
        if not has_content:
            continue
        if parent_key:
            node["parent_key"] = parent_key
        valid_add_nodes.append(node)
    return {
        "add_nodes": valid_add_nodes,
        "update_nodes": [item for item in update_nodes if item],
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
    shared_refs = _string_list(node.get("shared_refs") or [])
    key_findings = _string_list(node.get("key_findings") or [])
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
        "shared_refs": shared_refs,
        "key_findings": key_findings,
    }


def _normalize_node_update(node: dict) -> dict:
    status = node.get("status")
    if status is not None:
        status = _NODE_STATUS_ALIASES.get(str(status).strip().lower(), str(status).strip().lower())
    shared_refs = _string_list(node.get("shared_refs") or []) if node.get("shared_refs") is not None else None
    key_findings = _string_list(node.get("key_findings") or []) if node.get("key_findings") is not None else None
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
        "shared_refs": shared_refs,
        "key_findings": key_findings,
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


def _build_retry_context(
    *,
    role: str,
    attempt: int,
    max_attempts: int,
    error: Exception,
    attempt_trace: dict[str, object],
) -> dict[str, object]:
    failure_stage = "agent"
    previous_response_excerpt = ""
    if isinstance(error, LLMError):
        failure_stage = error.stage
        previous_response_excerpt = _excerpt(error.raw_text)
    elif isinstance(error, ValidationError):
        failure_stage = "schema_validation"
    response = attempt_trace.get("raw_response")
    if not previous_response_excerpt and response is not None:
        previous_response_excerpt = _excerpt(json.dumps(response, ensure_ascii=False))
    previous_action = None
    normalized = attempt_trace.get("normalized_response")
    if isinstance(normalized, dict):
        previous_action = normalized.get("action")
    return {
        "role": role,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "failure_stage": failure_stage,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "previous_response_excerpt": previous_response_excerpt,
        "previous_action": previous_action,
    }


def _excerpt(value: str, limit: int = 600) -> str:
    rendered = value.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."
