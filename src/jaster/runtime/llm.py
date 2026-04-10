from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

from jaster.runtime.json_extract import extract_json_object


class LLMError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "llm", raw_text: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.raw_text = raw_text


class OpenAIChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        reasoning_split: bool | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout if timeout is not None else _env_float("JASTER_HTTP_TIMEOUT", 120.0)
        self.max_retries = max_retries if max_retries is not None else _env_int("JASTER_LLM_MAX_RETRIES", 3)
        self.reasoning_split = _env_bool("OPENAI_REASONING_SPLIT", False)

        # HTTP retry settings
        self.http_max_retries = _env_int("JASTER_LLM_HTTP_MAX_RETRIES", 3)
        self.http_retry_base_delay = _env_float("JASTER_LLM_HTTP_RETRY_BASE_DELAY", 1.0)
        self.http_retry_max_delay = _env_float("JASTER_LLM_HTTP_RETRY_MAX_DELAY", 8.0)
        self.http_retry_jitter = _env_float("JASTER_LLM_HTTP_RETRY_JITTER", 0.2)

        # Rate limiting
        self.rate_limit_max_requests = _env_int("JASTER_LLM_RATE_LIMIT_MAX_REQUESTS", 2)
        self.rate_limit_window = _env_float("JASTER_LLM_RATE_LIMIT_WINDOW_SECONDS", 1.0)
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_timestamps: list[float] = []

        # Reusable httpx client
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _jitter(self) -> float:
        import random
        return random.uniform(-self.http_retry_jitter, self.http_retry_jitter)

    def _acquire_rate_limit(self) -> None:
        now = time.monotonic()
        with self._rate_limit_lock:
            cutoff = now - self.rate_limit_window
            self._rate_limit_timestamps = [t for t in self._rate_limit_timestamps if t > cutoff]
            if len(self._rate_limit_timestamps) >= self.rate_limit_max_requests:
                sleep_time = self._rate_limit_timestamps[0] - cutoff
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # Recompute after sleep
                self._rate_limit_timestamps = [t for t in self._rate_limit_timestamps if t > cutoff]
            self._rate_limit_timestamps.append(time.monotonic())

    def _post_with_retry(self, url: str, headers: dict, body: dict, mode: str) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.http_max_retries):
            client = self._get_client()
            try:
                response = client.post(url, headers=headers, json=body)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        delay = float(retry_after)
                    else:
                        delay = min(
                            self.http_retry_base_delay * (2 ** attempt) + self._jitter(),
                            self.http_retry_max_delay,
                        )
                    self._log_retry(mode, response.status_code, attempt + 1, self.http_max_retries, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                delay = min(
                    self.http_retry_base_delay * (2 ** attempt) + self._jitter(),
                    self.http_retry_max_delay,
                )
                self._log_retry(mode, None, attempt + 1, self.http_max_retries, delay)
                time.sleep(delay)
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                raise LLMError(f"LLM request failed: {exc}", stage="request") from exc
        raise LLMError(f"LLM request failed after {self.http_max_retries} retries", stage="request") from last_exc

    def complete_json(self, *, system: str, prompt: str) -> dict[str, Any]:
        body = self._build_body(system=system, prompt=prompt)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        self._log_request("json", body=body)
        self._acquire_rate_limit()
        try:
            response = self._post_with_retry(
                f"{self.base_url}/chat/completions", headers, body, "json"
            )
        except httpx.HTTPError as exc:
            self._log_failure("json", started, exc)
            raise
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            self._log_failure("json", started, exc)
            raise LLMError("LLM returned a non-JSON HTTP response", stage="response_json") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self._log_failure("json", started, exc)
            raise LLMError("LLM response body is missing choices[0].message.content", stage="response_shape") from exc
        self._log_success("json", started)
        return _extract_json(text)

    def complete_tool_call(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> dict[str, Any]:
        body = self._build_body(system=system, prompt=prompt)
        body["tools"] = tools
        body["tool_choice"] = {
            "type": "function",
            "function": {"name": tool_choice},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        self._log_request("tool_call", body=body, tool_choice=tool_choice)
        self._acquire_rate_limit()
        try:
            response = self._post_with_retry(
                f"{self.base_url}/chat/completions", headers, body, "tool_call"
            )
        except httpx.HTTPError as exc:
            self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
            raise
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
            raise LLMError("LLM returned a non-JSON HTTP response", stage="response_json") from exc
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
            raise LLMError("LLM response body is missing choices[0].message", stage="response_shape") from exc
        call: dict[str, Any]
        function: dict[str, Any]
        arguments: Any
        if isinstance(message, dict) and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
            try:
                call = message["tool_calls"][0]
                function = call["function"]
                arguments = function.get("arguments", "{}")
            except (KeyError, IndexError, TypeError) as exc:
                self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
                raise LLMError("LLM response body is missing tool_calls[0].function", stage="response_shape") from exc
        elif isinstance(message, dict) and isinstance(message.get("function_call"), dict):
            function = message["function_call"]
            call = {"id": "", "function": function}
            arguments = function.get("arguments", "{}")
        else:
            self._log_failure("tool_call", started, RuntimeError("missing tool call"), tool_choice=tool_choice)
            raise LLMError(
                "LLM response body is missing tool_calls[0].function or function_call",
                stage="response_shape",
                raw_text=json.dumps(message, ensure_ascii=False),
            )
        try:
            parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except (json.JSONDecodeError, TypeError) as exc:
            self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
            raise LLMError("LLM returned invalid tool call arguments JSON", stage="tool_arguments") from exc
        if function.get("name") != tool_choice:
            self._log_failure(
                "tool_call",
                started,
                RuntimeError(f"unexpected tool {function.get('name')}"),
                tool_choice=tool_choice,
            )
            raise LLMError(
                f"LLM called unexpected tool: {function.get('name')}",
                stage="tool_choice",
                raw_text=json.dumps(message, ensure_ascii=False),
            )
        self._log_success("tool_call", started, tool_choice=tool_choice)
        return {
            "id": call.get("id", ""),
            "name": function.get("name", ""),
            "arguments": parsed_arguments,
        }

    def _build_body(self, *, system: str, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if self.reasoning_split:
            body["reasoning_split"] = True
        return body

    def _log_request(self, mode: str, *, body: dict | None = None, tool_choice: str = "") -> None:
        extra = f" | tool={tool_choice}" if tool_choice else ""
        body_chars = len(json.dumps(body)) if body else 0
        print(
            f"[*] LLM request: mode={mode} | model={self.model} | timeout={self.timeout}s | body_chars={body_chars}{extra}",
            flush=True,
        )

    def _log_success(self, mode: str, started: float, *, tool_choice: str = "") -> None:
        extra = f" | tool={tool_choice}" if tool_choice else ""
        print(
            f"[*] LLM response: mode={mode} | elapsed={time.monotonic() - started:.2f}s{extra}",
            flush=True,
        )

    def _log_failure(self, mode: str, started: float, error: Exception, *, tool_choice: str = "") -> None:
        extra = f" | tool={tool_choice}" if tool_choice else ""
        print(
            f"[*] LLM error: mode={mode} | elapsed={time.monotonic() - started:.2f}s | error={type(error).__name__}: {error}{extra}",
            flush=True,
        )

    def _log_retry(self, mode: str, status_code: int | None, attempt: int, total: int, delay: float) -> None:
        status_str = f" status={status_code}" if status_code else " network error"
        print(
            f"[*] LLM retry: mode={mode}{status_str} | attempt={attempt}/{total} | backoff={delay:.1f}s",
            flush=True,
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _extract_json(text: Any) -> dict[str, Any]:
    try:
        return extract_json_object(text)
    except ValueError:
        rendered = str(text).strip()
        if "{" not in rendered or "}" not in rendered:
            raise LLMError("LLM did not return JSON", stage="json_extract", raw_text=rendered) from None
        raise LLMError("LLM returned invalid JSON", stage="json_extract", raw_text=rendered) from None
