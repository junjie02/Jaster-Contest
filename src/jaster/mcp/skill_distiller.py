from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any


class SkillDistiller:
    def __init__(self, llm_client, *, skills_root: Path | None = None) -> None:
        self.llm_client = llm_client
        self.skills_root = skills_root or Path(__file__).resolve().parents[3] / "skills" / "distilled"

    async def distill_and_update(self, payload: dict[str, Any]) -> Path:
        summary = str(payload.get("manual_insight") or "").strip()
        if not summary:
            raise ValueError("manual_insight is required")
        title_prompt = (
            "You are naming a pentest skill note. Return a short lowercase slug with hyphens only."
        )
        slug = await asyncio.to_thread(
            self.llm_client.complete_text,
            system=title_prompt,
            prompt=summary[:4000],
        )
        cleaned = re.sub(r"[^a-z0-9-]+", "-", slug.strip().lower()).strip("-") or "distilled-skill"
        path = self.skills_root / f"{cleaned}.md"
        self.skills_root.mkdir(parents=True, exist_ok=True)
        body = (
            f"---\n"
            f"name: {cleaned}\n"
            f"summary: Distilled field note generated from runtime learning.\n"
            f"use_when: Reuse this note when a similar exploitation pattern or bypass appears.\n"
            f"---\n\n"
            f"{summary}\n"
        )
        path.write_text(body, encoding="utf-8")
        return path
