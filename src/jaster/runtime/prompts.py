from __future__ import annotations

from pathlib import Path
from string import Template


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def render(self, role: str, *, zone: str, payload_json: str) -> str:
        shared = self._read("shared.md")
        agent = self._read(f"agents/{role}.md")
        zone_text = self._read(f"zones/{zone}.md", optional=True)
        template = "\n\n".join(part for part in [shared, agent, zone_text] if part.strip())
        return Template(template).safe_substitute(payload_json=payload_json, zone=zone, role=role)

    def _read(self, relative: str, optional: bool = False) -> str:
        path = self.root / relative
        if not path.exists():
            if optional:
                return ""
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")

