from __future__ import annotations

import json
from typing import Any


def extract_json_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, list):
        rendered = "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in payload
        )
    else:
        rendered = str(payload)
    rendered = rendered.strip()
    if not rendered:
        raise ValueError("empty payload")
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError:
        start = rendered.find("{")
        end = rendered.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("payload does not contain a JSON object") from None
        parsed = json.loads(rendered[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("payload did not decode to a JSON object")
    return parsed
