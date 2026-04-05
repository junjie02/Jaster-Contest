from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jaster.domain import ArtifactRef, AvailableSkill, ExecutionResult


class SkillSpec(AvailableSkill):
    command_mode: str = "argv"
    bin: str = ""


class SkillCatalog:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._specs = self._load_specs()

    def list_available(self) -> list[AvailableSkill]:
        return [AvailableSkill(name=spec.name, summary=spec.summary, use_when=spec.use_when) for spec in self._specs.values()]

    def get(self, name: str) -> SkillSpec | None:
        return self._specs.get(name)

    def _load_specs(self) -> dict[str, SkillSpec]:
        specs: dict[str, SkillSpec] = {}
        if not self.skills_dir.exists():
            return specs
        for path in sorted(self.skills_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            spec = SkillSpec.model_validate(payload)
            specs[spec.name] = spec
        return specs


class SkillExecutor:
    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def run(self, skill_name: str, skill_args: dict[str, Any], *, cwd: Path) -> ExecutionResult:
        spec = self.catalog.get(skill_name)
        if spec is None:
            return ExecutionResult(success=False, summary=f"Unknown skill: {skill_name}", stderr="unknown skill")
        if not shutil.which(spec.bin):
            return ExecutionResult(success=False, summary=f"Skill binary not found: {spec.bin}", stderr="missing binary")
        command = self._build_command(spec, skill_args)
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        artifacts = [ArtifactRef(kind="work_dir", path=str(cwd))]
        summary = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        return ExecutionResult(
            success=completed.returncode == 0,
            summary=summary or f"Skill {skill_name} finished",
            findings=[line for line in completed.stdout.splitlines() if line.strip()][:10],
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            command=" ".join(command),
            artifacts=artifacts,
        )

    def _build_command(self, spec: SkillSpec, skill_args: dict[str, Any]) -> list[str]:
        if spec.name == "system_command":
            return [spec.bin, "-lc", str(skill_args.get("command", ""))]
        command = [spec.bin]
        for key, value in skill_args.items():
            if value is None or value is False:
                continue
            flag = f"--{key.replace('_', '-')}"
            if value is True:
                command.append(flag)
            else:
                command.extend([flag, str(value)])
        return command

