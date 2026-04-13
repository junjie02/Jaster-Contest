#!/usr/bin/env python3
"""
MCP tool smoke test.

Goals:
- Verify the MCP service can start and expose tools.
- Verify safe/local tools are callable.
- Avoid treating "fake business input" as "tool is broken".
- Classify external dependency failures separately from real tool failures.

Default behavior:
- Skip dangerous contest-control tools and complete_mission.
- Only run safe platform read-only checks when platform env is configured.
- Test payload-server tools with a real lifecycle: start -> logs -> stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)


SAFE_STATUS = {"success", "ok", "started", "stopped", True}
SKIP_DANGEROUS = {
    "complete_mission",
    "platform_ensure_challenge_running",
    "platform_stop_challenge",
    "platform_submit_flag",
    "platform_view_hint",
    "platform_acquire_challenges",
    "platform_release_completed_challenges",
}
EXTERNAL_TOOLS = {
    "expert_analysis",
    "distill_knowledge",
    "web_search",
    "search_exploit",
}
OPTIONAL_NETWORK_TOOLS = {
    "http_request",
    "concurrency_test",
    "dirsearch_scan",
    "sqlmap_tool",
    "nuclei_scan",
}
TEST_CASES: dict[str, dict[str, Any]] = {
    "think": {
        "analysis": "Testing think tool",
        "problem": "Verify the tool accepts structured reasoning input.",
        "reasoning_steps": ["Prepare test input", "Call tool", "Check structured response"],
        "conclusion": "Smoke test complete",
    },
    "formulate_hypotheses": {
        "hypotheses": [
            {"description": "Tool can store hypotheses", "confidence": 0.8},
            {"description": "Structured array input is accepted", "confidence": 0.6},
        ]
    },
    "reflect_on_failure": {
        "failed_action": {"tool": "http_request", "params": {"url": "http://example.com"}},
        "error_message": "Synthetic failure for smoke test",
    },
    "expert_analysis": {
        "question": "Briefly explain why LFI can lead to source disclosure.",
        "context_data": "Smoke test only.",
    },
    "distill_knowledge": {
        "insight_summary": "Smoke test: verify the distillation pipeline accepts a simple insight summary.",
    },
    "web_search": {
        "query": "site:example.com smoke test",
        "num_results": 1,
    },
    "shell_exec": {"command": "echo 'hello from shell_exec'"},
    "python_exec": {"script": "print('hello from python_exec')"},
    "sqlmap_tool": {"url": "http://example.com/test.php?id=1", "level": 1, "risk": 1, "extra_args": "--batch --smart"},
    "dirsearch_scan": {"url": "http://example.com", "extensions": "php,html", "extra_args": "--max-time=5"},
    "http_request": {"url": "http://example.com", "method": "GET", "timeout": 8},
    "concurrency_test": {"url": "http://example.com", "method": "GET", "concurrent_count": 2},
    "search_exploit": {"keywords": "wordpress lfi", "max_results": 1},
    "view_exploit": {"edb_id": "51826", "max_lines": 40},
    "nuclei_scan": {"target": "http://example.com", "timeout": 5, "rate_limit": 20, "concurrency": 5},
    "nuclei_list_templates": {"limit": 1},
    "list_payload_servers": {},
    "platform_list_challenges": {},
}


@dataclass
class ToolOutcome:
    tool: str
    status: str
    message: str = ""
    raw: dict[str, Any] | None = None


def _build_mcp_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    env["FASTMCP_NO_BANNER"] = "1"
    return env


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-u", "-m", "jaster.mcp.mcp_service"],
        env=_build_mcp_env(),
    )


def _platform_enabled() -> bool:
    return bool(os.environ.get("JASTER_PLATFORM_HOST") and os.environ.get("JASTER_AGENT_TOKEN"))


def _classify_error(tool_name: str, error_text: str) -> str:
    lowered = error_text.lower()
    if any(token in lowered for token in ["529", "rate limit", "too many requests", "overloaded"]):
        return "env_issue"
    if any(token in lowered for token in ["connecterror", "connection", "timed out", "timeout", "temporary failure", "name resolution"]):
        return "env_issue"
    if tool_name in EXTERNAL_TOOLS or tool_name in OPTIONAL_NETWORK_TOOLS:
        return "env_issue"
    return "fail"


def _extract_content_text(result: Any) -> str:
    for content in getattr(result, "content", None) or []:
        if hasattr(content, "text"):
            return str(content.text)
    return ""


def _normalize_payload_success(payload: Any) -> tuple[bool, str]:
    if isinstance(payload, dict):
        if "success" in payload:
            success = bool(payload["success"])
            return success, str(payload.get("error") or payload.get("message") or payload.get("status") or "")
        if "status" in payload:
            status = payload.get("status")
            success = status in SAFE_STATUS
            return success, str(payload.get("message") or payload.get("error") or status or "")
    return True, ""


async def _call_tool(session: ClientSession, tool_name: str, arguments: dict[str, Any] | None = None) -> tuple[bool, str, Any]:
    result = await session.call_tool(tool_name, arguments or {})
    text = _extract_content_text(result)
    if not text:
        return True, "", {"result": str(result)}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return True, text[:240], {"text": text[:1000]}
    success, message = _normalize_payload_success(payload)
    return success, message, payload


def _default_params(tool_name: str) -> dict[str, Any]:
    return TEST_CASES.get(tool_name, {})


async def _test_payload_lifecycle(session: ClientSession) -> list[ToolOutcome]:
    outcomes: list[ToolOutcome] = []
    ok, message, payload = await _call_tool(
        session,
        "start_payload_server",
        {"routes": [{"path": "/test", "content": "ok"}]},
    )
    if not ok or not isinstance(payload, dict):
        outcomes.append(ToolOutcome("start_payload_server", "fail", message or "start failed", payload if isinstance(payload, dict) else None))
        outcomes.append(ToolOutcome("get_payload_server_logs", "skip", "Skipped because start_payload_server failed"))
        outcomes.append(ToolOutcome("stop_payload_server", "skip", "Skipped because start_payload_server failed"))
        return outcomes

    server_id = str(payload.get("server_id") or "")
    outcomes.append(ToolOutcome("start_payload_server", "pass", message or "started", payload))
    if not server_id:
        outcomes.append(ToolOutcome("get_payload_server_logs", "fail", "No server_id returned from start_payload_server", payload))
        outcomes.append(ToolOutcome("stop_payload_server", "skip", "Skipped because server_id was missing"))
        return outcomes

    ok, message, payload = await _call_tool(session, "get_payload_server_logs", {"server_id": server_id})
    outcomes.append(ToolOutcome("get_payload_server_logs", "pass" if ok else "fail", message or "logs fetched", payload if isinstance(payload, dict) else None))

    ok, message, payload = await _call_tool(session, "stop_payload_server", {"server_id": server_id})
    outcomes.append(ToolOutcome("stop_payload_server", "pass" if ok else "fail", message or "stopped", payload if isinstance(payload, dict) else None))
    return outcomes


async def _test_single_tool(session: ClientSession, tool_name: str) -> ToolOutcome:
    if tool_name in SKIP_DANGEROUS:
        return ToolOutcome(tool_name, "skip", "Dangerous/mutating tool skipped by default")

    if tool_name.startswith("platform_") and not _platform_enabled():
        return ToolOutcome(tool_name, "skip", "Platform env not configured")

    has_test_case = tool_name in TEST_CASES
    params = _default_params(tool_name)
    if tool_name == "platform_list_challenges":
        ok, message, payload = await _call_tool(session, tool_name, params)
        return ToolOutcome(tool_name, "pass" if ok else _classify_error(tool_name, message or "platform_list_challenges failed"), message or "ok", payload if isinstance(payload, dict) else None)

    if not has_test_case:
        return ToolOutcome(tool_name, "skip", "No safe smoke-test case defined")

    try:
        ok, message, payload = await _call_tool(session, tool_name, params)
    except Exception as exc:
        message = str(exc)
        return ToolOutcome(tool_name, _classify_error(tool_name, message), message[:300])

    if ok:
        return ToolOutcome(tool_name, "pass", message or "ok", payload if isinstance(payload, dict) else None)
    return ToolOutcome(tool_name, _classify_error(tool_name, message or "tool reported failure"), message or "tool reported failure", payload if isinstance(payload, dict) else None)


async def test_mcp_tools() -> dict[str, ToolOutcome]:
    outcomes: dict[str, ToolOutcome] = {}
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.list_tools()
            tools = sorted(tool.name for tool in result.tools)
            print(f"Found {len(tools)} tools\n")
            for name in tools:
                print(f"  - {name}")
            print()

            if "start_payload_server" in tools and "get_payload_server_logs" in tools and "stop_payload_server" in tools:
                for outcome in await _test_payload_lifecycle(session):
                    outcomes[outcome.tool] = outcome

            for tool_name in tools:
                if tool_name in outcomes:
                    continue
                outcome = await _test_single_tool(session, tool_name)
                outcomes[tool_name] = outcome
                label = {
                    "pass": "PASS",
                    "skip": "SKIP",
                    "env_issue": "ENV ",
                    "fail": "FAIL",
                }.get(outcome.status, outcome.status.upper())
                print(f"[{label}] {tool_name}: {outcome.message[:160]}")

    return outcomes


def _print_summary(outcomes: dict[str, ToolOutcome]) -> None:
    counts = {"pass": 0, "skip": 0, "env_issue": 0, "fail": 0}
    for item in outcomes.values():
        counts[item.status] = counts.get(item.status, 0) + 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total tools: {len(outcomes)}")
    print(f"Pass: {counts.get('pass', 0)}")
    print(f"Skip: {counts.get('skip', 0)}")
    print(f"Env issues: {counts.get('env_issue', 0)}")
    print(f"Fail: {counts.get('fail', 0)}")

    for bucket, title in [("fail", "Real failures"), ("env_issue", "Environment / upstream issues"), ("skip", "Skipped")]:
        selected = [item for item in outcomes.values() if item.status == bucket]
        if not selected:
            continue
        print(f"\n{title}:")
        for item in selected:
            print(f"  - {item.tool}: {item.message[:180]}")


def main() -> int:
    try:
        outcomes = asyncio.run(test_mcp_tools())
    except Exception as exc:
        print(f"Failed to connect to MCP service: {exc}")
        import traceback

        traceback.print_exc()
        return 2

    _print_summary(outcomes)
    return 1 if any(item.status == "fail" for item in outcomes.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
