from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from jaster.runtime.platform import (
    ChallengeListData,
    HintResult,
    PlatformAPIError,
    PlatformChallenge,
    PlatformRateLimiter,
    StartChallengeResult,
    SubmitResult,
)


class ContestMcpClient:
    def __init__(
        self,
        *,
        url: str,
        agent_token: str,
        timeout: float = 20.0,
        rate_limiter: PlatformRateLimiter | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.agent_token = agent_token.strip()
        self.timeout = timeout
        self.rate_limiter = rate_limiter or PlatformRateLimiter()

    @property
    def base_url(self) -> str:
        return self.url

    def list_challenges(self) -> ChallengeListData:
        payload = self._call_tool_sync("list_challenges", {})
        return self._parse_list_challenges(payload)

    def start_challenge(self, code: str) -> StartChallengeResult:
        payload = self._call_tool_sync("start_challenge", {"code": code})
        return self._parse_start_challenge(payload)

    def stop_challenge(self, code: str) -> None:
        self._call_tool_sync("stop_challenge", {"code": code})

    def submit_flag(self, code: str, flag: str) -> SubmitResult:
        payload = self._call_tool_sync("submit_flag", {"code": code, "flag": flag})
        return self._parse_submit_flag(payload)

    def view_hint(self, code: str) -> HintResult:
        payload = self._call_tool_sync("view_hint", {"code": code})
        return self._parse_view_hint(code, payload)

    def _call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.rate_limiter.acquire()
        try:
            return asyncio.run(self._call_tool(tool_name, arguments))
        except PlatformAPIError:
            raise
        except Exception as exc:
            raise PlatformAPIError(f"MCP工具 {tool_name} 调用失败: {exc}") from exc

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.agent_token}"}
        async with streamablehttp_client(self.url, headers=headers, timeout=self.timeout) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
        if getattr(result, "isError", False):
            message = self._extract_result_text(result) or "MCP tool returned an error."
            raise PlatformAPIError(message)
        text = self._extract_result_text(result)
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                raise PlatformAPIError(f"MCP工具 {tool_name} 返回了非 JSON 文本: {text[:200]}")
            if isinstance(payload, dict) and payload.get("success") is False:
                raise PlatformAPIError(str(payload.get("error") or payload.get("message") or f"MCP工具 {tool_name} 执行失败"))
            return payload if isinstance(payload, dict) else {"data": payload}
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            if structured.get("success") is False:
                raise PlatformAPIError(str(structured.get("error") or structured.get("message") or f"MCP工具 {tool_name} 执行失败"))
            return structured
        raise PlatformAPIError(f"MCP工具 {tool_name} 返回为空")

    @staticmethod
    def _extract_result_text(result: Any) -> str:
        for item in getattr(result, "content", None) or []:
            if hasattr(item, "text"):
                return str(item.text)
        return ""

    def _parse_list_challenges(self, payload: dict[str, Any]) -> ChallengeListData:
        current_level = int(payload.get("current_level") or 0)
        total_challenges = int(payload.get("total_challenges") or payload.get("total_visible") or 0)
        solved_challenges = int(payload.get("solved_challenges") or 0)
        raw_challenges = payload.get("challenges") or []
        challenges: list[PlatformChallenge] = []
        for item in raw_challenges:
            if not isinstance(item, dict):
                continue
            challenges.append(
                PlatformChallenge(
                    title=str(item.get("title") or ""),
                    code=str(item.get("code") or ""),
                    difficulty=str(item.get("difficulty") or item.get("level_name") or ""),
                    description=str(item.get("description") or ""),
                    level=int(item.get("level") or current_level or 0),
                    total_score=int(item.get("total_score") or 0),
                    total_got_score=int(item.get("total_got_score") or 0),
                    flag_count=int(item.get("flag_count") or 0),
                    flag_got_count=int(item.get("flag_got_count") or 0),
                    hint_viewed=bool(item.get("hint_viewed") or item.get("is_hint_viewed") or False),
                    instance_status=str(item.get("instance_status") or item.get("status") or ""),
                    entrypoint=self._to_entrypoints(item.get("entrypoint") or item.get("entrypoints") or item.get("target")),
                )
            )
        if not total_challenges:
            total_challenges = len(challenges)
        if not solved_challenges:
            solved_challenges = sum(1 for item in challenges if item.flag_count > 0 and item.flag_got_count >= item.flag_count)
        return ChallengeListData(
            current_level=current_level,
            total_challenges=total_challenges,
            solved_challenges=solved_challenges,
            challenges=challenges,
        )

    def _parse_start_challenge(self, payload: dict[str, Any]) -> StartChallengeResult:
        if payload.get("already_completed"):
            return StartChallengeResult(already_completed=True)
        entrypoints = self._to_entrypoints(
            payload.get("entrypoint")
            or payload.get("entrypoints")
            or payload.get("target")
            or payload.get("data")
        )
        return StartChallengeResult(entrypoints=entrypoints, already_completed=False)

    def _parse_submit_flag(self, payload: dict[str, Any]) -> SubmitResult:
        return SubmitResult(
            correct=bool(payload.get("correct")),
            message=str(payload.get("message") or ""),
            flag_count=int(payload.get("flag_count") or 0),
            flag_got_count=int(payload.get("flag_got_count") or 0),
        )

    def _parse_view_hint(self, code: str, payload: dict[str, Any]) -> HintResult:
        return HintResult(
            code=str(payload.get("code") or code),
            hint_content=str(payload.get("hint_content") or payload.get("hint") or "") or None,
        )

    @staticmethod
    def _to_entrypoints(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            rendered = value.strip()
            return [rendered] if rendered else []
        return []
