from __future__ import annotations

import json
import os
from typing import Any

import httpx


class LLMError(RuntimeError):
    pass


class OpenAIChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.max_retries = max_retries

    def complete_json(self, *, system: str, prompt: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        with httpx.Client(timeout=self.timeout) as client:
            for _ in range(self.max_retries):
                try:
                    response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                    response.raise_for_status()
                    payload = response.json()
                    text = payload["choices"][0]["message"]["content"]
                    return _extract_json(text)
                except Exception as exc:  # pragma: no cover - exercised through tests with fake client path
                    last_error = exc
        raise LLMError(f"LLM request failed: {last_error}")


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
            raise LLMError("LLM did not return JSON")
        try:
            return json.loads(rendered[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON") from exc

