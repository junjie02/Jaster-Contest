from __future__ import annotations

import json
import os
from typing import Any

import httpx


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
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM request failed: {exc}", stage="request") from exc
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise LLMError("LLM returned a non-JSON HTTP response", stage="response_json") from exc
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("LLM response body is missing choices[0].message.content", stage="response_shape") from exc
            return _extract_json(text)

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
    if isinstance(text, list):
        rendered = "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in text
        )
    else:
        rendered = str(text)
    rendered = rendered.strip()
    try:
        return json.loads(rendered)
    except json.JSONDecodeError:
        start = rendered.find("{")
        end = rendered.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError("LLM did not return JSON", stage="json_extract", raw_text=rendered)
        try:
            return json.loads(rendered[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON", stage="json_extract", raw_text=rendered) from exc
