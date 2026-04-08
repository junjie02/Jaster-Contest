from __future__ import annotations

import time
from collections import deque
from typing import Any

import httpx
from pydantic import BaseModel, Field


class PlatformAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        payload_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload_code = payload_code
        self.retryable = retryable


class PlatformChallenge(BaseModel):
    title: str
    code: str
    difficulty: str
    description: str = ""
    level: int
    total_score: int = 0
    total_got_score: int = 0
    flag_count: int = 0
    flag_got_count: int = 0
    hint_viewed: bool = False
    instance_status: str = ""
    entrypoint: list[str] | None = None


class ChallengeListData(BaseModel):
    current_level: int = 0
    total_challenges: int = 0
    solved_challenges: int = 0
    challenges: list[PlatformChallenge] = Field(default_factory=list)


class StartChallengeResult(BaseModel):
    entrypoints: list[str] = Field(default_factory=list)
    already_completed: bool = False


class SubmitResult(BaseModel):
    correct: bool
    message: str = ""
    flag_count: int = 0
    flag_got_count: int = 0


class HintResult(BaseModel):
    code: str = ""
    hint_content: str | None = None


class PlatformRateLimiter:
    def __init__(
        self,
        *,
        max_requests: int = 3,
        window_seconds: float = 1.0,
        time_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._time_fn = time_fn or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        now = self._time_fn()
        self._trim(now)
        if len(self._timestamps) >= self.max_requests:
            wait_seconds = self.window_seconds - (now - self._timestamps[0])
            if wait_seconds > 0:
                self._sleep_fn(wait_seconds)
            now = self._time_fn()
            self._trim(now)
        self._timestamps.append(self._time_fn())

    def _trim(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
            self._timestamps.popleft()


class PlatformClient:
    def __init__(
        self,
        *,
        base_url: str,
        agent_token: str,
        http_client: httpx.Client | None = None,
        timeout: float = 15.0,
        rate_limiter: PlatformRateLimiter | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_token = agent_token
        self.timeout = timeout
        self.rate_limiter = rate_limiter or PlatformRateLimiter()
        self._sleep_fn = sleep_fn or time.sleep
        self._client = http_client

    def list_challenges(self) -> ChallengeListData:
        payload = self._request("GET", "/challenges")
        return ChallengeListData.model_validate(payload)

    def start_challenge(self, code: str) -> StartChallengeResult:
        payload = self._request("POST", "/start_challenge", json_body={"code": code})
        if isinstance(payload, dict) and payload.get("already_completed"):
            return StartChallengeResult(already_completed=True)
        if isinstance(payload, list):
            return StartChallengeResult(entrypoints=[str(item) for item in payload])
        raise PlatformAPIError("平台返回了无法识别的启动结果", status_code=200)

    def stop_challenge(self, code: str) -> None:
        self._request("POST", "/stop_challenge", json_body={"code": code})

    def submit_flag(self, code: str, flag: str) -> SubmitResult:
        payload = self._request("POST", "/submit", json_body={"code": code, "flag": flag})
        return SubmitResult.model_validate(payload)

    def view_hint(self, code: str) -> HintResult:
        payload = self._request("POST", "/hint", json_body={"code": code})
        return HintResult.model_validate(payload)

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        attempts = 0
        while True:
            attempts += 1
            self.rate_limiter.acquire()
            response = self._send(method, path, json_body=json_body)
            try:
                payload = response.json()
            except ValueError as exc:
                raise PlatformAPIError(
                    f"平台返回了非 JSON 响应: HTTP {response.status_code}",
                    status_code=response.status_code,
                ) from exc

            retryable = self._is_retryable(response.status_code, payload)
            if retryable:
                delays = self._retry_delays(response.status_code)
                if attempts <= len(delays):
                    self._sleep_fn(delays[attempts - 1])
                    continue
            self._raise_for_error(response.status_code, payload, retryable=retryable)
            return payload.get("data")

    def _send(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> httpx.Response:
        headers = {"Agent-Token": self.agent_token}
        if self._client is not None:
            return self._client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body)
        with httpx.Client(timeout=self.timeout) as client:
            return client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body)

    def _is_retryable(self, status_code: int, payload: dict[str, Any]) -> bool:
        message = str(payload.get("message", ""))
        return (
            status_code == 429
            or status_code in {502, 503}
            or (status_code == 400 and "切换" in message)
        )

    def _retry_delays(self, status_code: int) -> list[float]:
        if status_code in {429, 400}:
            return [0.5, 1.0, 2.0, 2.0, 2.0]
        if status_code in {502, 503}:
            return [1.0, 2.0, 4.0]
        return []

    def _raise_for_error(self, status_code: int, payload: dict[str, Any], *, retryable: bool) -> None:
        payload_code = payload.get("code")
        if status_code == 200 and payload_code == 0:
            return
        raise PlatformAPIError(
            str(payload.get("message") or f"平台请求失败: HTTP {status_code}"),
            status_code=status_code,
            payload_code=payload_code if isinstance(payload_code, int) else None,
            retryable=retryable,
        )
