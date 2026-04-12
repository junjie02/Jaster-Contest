from __future__ import annotations

import json
import sys
from typing import Generic, TypeVar

from pydantic import BaseModel

from jaster.runtime.prompts import PromptLibrary

InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)

STRICT_JSON_SYSTEM = (
    "你是一个严格的json生成器.遵循角色指令并严格返回json字段.请注意你自身的身份，只做你应该做的事。"
)

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"


class JsonAgent(Generic[InputModel, OutputModel]):
    role: str
    input_model: type[InputModel]
    output_model: type[OutputModel]

    def __init__(self, llm, prompts: PromptLibrary) -> None:
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
            prompt = self.prompts.render(self.role, zone=zone, payload_json=payload_json)
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
                if attempt > 1:
                    _log_agent_retry_recovered(self.role, attempt, max_attempts)
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
                _log_agent_retry(self.role, attempt, max_attempts, exc)
                current_retry_context = _build_retry_context(
                    role=self.role,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=exc,
                    attempt_trace=attempt_trace,
                )
                if attempt >= max_attempts:
                    _log_agent_retry_exhausted(self.role, attempt, max_attempts, exc)
                    self.last_trace = {
                        "role": self.role,
                        "zone": zone,
                        "system": STRICT_JSON_SYSTEM,
                        "attempts": attempts,
                        "succeeded_attempt": None,
                    }
                    raise
        raise RuntimeError("unreachable")


def _normalize_agent_response(role: str, payload: dict) -> dict:
    normalized = dict(payload or {})
    if role == "plan":
        normalized.setdefault("phase_summary", str(normalized.get("phase_summary") or normalized.get("summary") or ""))
        normalized.setdefault("planner_notes", str(normalized.get("planner_notes") or normalized.get("notes") or ""))
        normalized["dispatch_task_keys"] = _string_list(
            normalized.get("dispatch_task_keys") or normalized.get("dispatch_keys") or normalized.get("task_keys") or []
        )
        normalized["tree_patch"] = _normalize_task_tree_patch(normalized.get("tree_patch") or {})
    elif role == "strategy":
        normalized.setdefault("phase_summary", str(normalized.get("phase_summary") or normalized.get("summary") or ""))
        normalized["is_complete"] = bool(normalized.get("is_complete", normalized.get("completed", False)))
        normalized.setdefault("task_summary", str(normalized.get("task_summary") or normalized.get("summary") or ""))
        normalized["task_findings"] = _string_list(
            normalized.get("task_findings") or normalized.get("findings") or []
        )
        normalized["flag_candidates"] = _string_list(
            normalized.get("flag_candidates") or normalized.get("flags_found") or []
        )
        normalized["observed_task_results"] = normalized.get("observed_task_results") or []
        normalized["credentials"] = _string_list(normalized.get("credentials") or [])
        normalized["actions"] = _normalize_actions(normalized)
    elif role == "reflection":
        normalized.setdefault("summary", str(normalized.get("summary") or normalized.get("phase_summary") or ""))
        normalized.setdefault(
            "planner_guidance",
            str(normalized.get("planner_guidance") or normalized.get("guidance") or normalized.get("notes") or ""),
        )
        normalized["flag_candidates"] = _string_list(
            normalized.get("flag_candidates") or normalized.get("flags_found") or []
        )
        normalized["credentials"] = _string_list(normalized.get("credentials") or [])
        normalized["task_updates"] = _normalize_task_updates(normalized.get("task_updates") or [])
    elif role == "submission":
        normalized["flag"] = normalized.get("flag", normalized.get("answer"))
        normalized.setdefault("reason", str(normalized.get("reason") or normalized.get("summary") or ""))
    return normalized


def _normalize_actions(payload: dict) -> list[dict]:
    raw_actions = payload.get("actions")
    if raw_actions is None:
        raw_actions = payload.get("action")
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    if not isinstance(raw_actions, list):
        raw_actions = []

    normalized: list[dict] = []
    seen_task_ids: set[str] = set()
    finish_count = 0
    for index, item in enumerate(raw_actions, start=1):
        if not isinstance(item, dict):
            continue
        action = _normalize_action(item)
        task_id = str(action.get("task_id") or f"task{index}").strip() or f"task{index}"
        while task_id in seen_task_ids:
            task_id = f"{task_id}_{index}"
        action["task_id"] = task_id
        seen_task_ids.add(task_id)
        if action.get("kind") == "finish":
            finish_count += 1
        normalized.append(action)

    if not normalized:
        normalized = [_normalize_action({"task_id": "task1", "kind": "finish", "goal": "Stop current task."})]
    if finish_count and len(normalized) > 1:
        raise ValueError("finish action must be the only action in actions")
    return normalized


def _normalize_action(action: dict) -> dict:
    source = dict(action or {})
    normalized: dict[str, object] = {}
    normalized["task_id"] = str(source.get("task_id") or "").strip()
    requested_kind = str(source.get("kind") or "").strip().lower()
    tool_name = source.get("tool_name") or source.get("tool") or source.get("name")
    if requested_kind == "finish":
        normalized["kind"] = "finish"
    elif requested_kind == "tool" or tool_name:
        normalized["kind"] = "tool"
    else:
        normalized["kind"] = "finish"
    normalized["goal"] = str(source.get("goal") or source.get("reason") or "Continue the assigned task.")
    normalized["expected_result"] = str(
        source.get("expected_result") or source.get("expected_output") or ""
    )
    normalized["tool_name"] = tool_name
    normalized["tool_args"] = source.get("tool_args") or source.get("params") or source.get("arguments") or {}
    if normalized["kind"] == "finish":
        normalized["tool_name"] = None
        normalized["tool_args"] = {}
    if normalized["kind"] == "tool" and not normalized["tool_name"]:
        normalized["kind"] = "finish"
        normalized["tool_args"] = {}
    return normalized


def _normalize_task_tree_patch(payload: dict) -> dict:
    source = dict(payload or {})
    add_nodes = []
    update_nodes = []
    for item in source.get("add_nodes", []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        add_nodes.append(
            {
                "parent_key": str(item.get("parent_key") or ""),
                "title": title,
                "reason": str(item.get("reason") or ""),
                "completion_criteria": str(item.get("completion_criteria") or item.get("done_when") or ""),
                "status": str(item.get("status") or "in_progress"),
                "latest_summary": str(item.get("latest_summary") or ""),
                "latest_findings": _string_list(item.get("latest_findings") or []),
                "attempt_count": _normalize_int(item.get("attempt_count")),
            }
        )
    for item in source.get("update_nodes", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("task_key") or "").strip()
        if not key:
            continue
        update_nodes.append(
            {
                "key": key,
                "title": item.get("title"),
                "reason": item.get("reason"),
                "completion_criteria": item.get("completion_criteria") or item.get("done_when"),
                "status": item.get("status"),
                "latest_summary": item.get("latest_summary"),
                "latest_findings": _string_list(item.get("latest_findings") or [])
                if item.get("latest_findings") is not None
                else None,
                "attempt_count": _normalize_int(item.get("attempt_count")) if item.get("attempt_count") is not None else None,
            }
        )
    return {"add_nodes": add_nodes, "update_nodes": update_nodes}


def _normalize_task_updates(raw_updates: object) -> list[dict]:
    if isinstance(raw_updates, dict):
        raw_updates = [raw_updates]
    if not isinstance(raw_updates, list):
        raw_updates = []
    normalized: list[dict] = []
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("task_key") or "").strip()
        if not key:
            continue
        status = str(item.get("status") or "in_progress").strip().lower() or "in_progress"
        if status not in {"in_progress", "completed", "failed"}:
            status = "in_progress"
        normalized.append(
            {
                "key": key,
                "status": status,
                "latest_summary": str(item.get("latest_summary") or item.get("summary") or ""),
                "latest_findings": _string_list(item.get("latest_findings") or item.get("findings") or []),
                "reason": str(item.get("reason") or ""),
            }
        )
    return normalized


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        rendered = str(item).strip()
        if rendered and rendered not in normalized:
            normalized.append(rendered)
    return normalized


def _normalize_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _style(text: str, color: str = "", *, bold: bool = False) -> str:
    if not sys.stdout.isatty():
        return text
    prefix = ""
    if bold:
        prefix += ANSI_BOLD
    if color:
        prefix += color
    return f"{prefix}{text}{ANSI_RESET}" if prefix else text


def _short_error(error: Exception, *, limit: int = 220) -> str:
    message = f"{type(error).__name__}: {error}"
    message = " ".join(message.split())
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


def _log_agent_retry(role: str, attempt: int, max_attempts: int, error: Exception) -> None:
    if attempt >= max_attempts:
        return
    print(
        f"    {_style(f'[{role}:retry {attempt}/{max_attempts}]', ANSI_YELLOW, bold=True)} {_short_error(error)}",
        flush=True,
    )


def _log_agent_retry_recovered(role: str, attempt: int, max_attempts: int) -> None:
    print(
        f"    {_style(f'[{role}:retry recovered {attempt}/{max_attempts}]', ANSI_GREEN, bold=True)} output accepted",
        flush=True,
    )


def _log_agent_retry_exhausted(role: str, attempt: int, max_attempts: int, error: Exception) -> None:
    print(
        f"    {_style(f'[{role}:retry exhausted {attempt}/{max_attempts}]', ANSI_RED, bold=True)} {_short_error(error)}",
        flush=True,
    )


def _build_retry_context(
    *,
    role: str,
    attempt: int,
    max_attempts: int,
    error: Exception,
    attempt_trace: dict[str, object],
) -> dict[str, object]:
    return {
        "role": role,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "last_prompt_excerpt": str(attempt_trace.get("prompt", ""))[:2000],
        "last_response": attempt_trace.get("raw_response"),
    }
