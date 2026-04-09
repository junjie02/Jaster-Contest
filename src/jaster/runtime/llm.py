from __future__ import annotations

import json
import os
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
        if reasoning_split is None:
            reasoning_split = _env_bool(
                "OPENAI_REASONING_SPLIT",
                self.model.lower().startswith("minimax"),
            )
        self.reasoning_split = reasoning_split

    def complete_json(self, *, system: str, prompt: str) -> dict[str, Any]:
        body = self._build_body(system=system, prompt=prompt)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        self._log_request("json")
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self._log_failure("json", started, exc)
                raise LLMError(f"LLM request failed: {exc}", stage="request") from exc
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
        self._log_request("tool_call", tool_choice=tool_choice)
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self._log_failure("tool_call", started, exc, tool_choice=tool_choice)
                raise LLMError(f"LLM request failed: {exc}", stage="request") from exc
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
        body = {
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

    def _log_request(self, mode: str, *, tool_choice: str = "") -> None:
        extra = f" | tool={tool_choice}" if tool_choice else ""
        print(
            f"[*] LLM request: mode={mode} | model={self.model} | timeout={self.timeout}s{extra}",
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
