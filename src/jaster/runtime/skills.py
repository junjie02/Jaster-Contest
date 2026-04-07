from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from jaster.domain import ArtifactRef, AvailableSkill, ExecutionResult


class SkillArgSpec(BaseModel):
    name: str
    type: Literal["string", "int", "bool", "string_list", "int_list"] = "string"
    flag: str = ""
    position: int | None = None
    repeatable: bool = False
    required: bool = False
    default: Any = None
    enum: list[Any] = Field(default_factory=list)
    path_policy: Literal["none", "work_dir_relative", "work_dir_output"] = "none"


class SkillSpec(AvailableSkill):
    command_mode: Literal["argv", "shell"] = "argv"
    bin: str = ""
    base_argv: list[str] = Field(default_factory=list)
    primary_locator_arg: str = ""
    shape_signature_args: list[str] = Field(default_factory=list)
    variant_signature_args: list[str] = Field(default_factory=list)
    bin_selector_arg: str = ""
    bin_map: dict[str, str] = Field(default_factory=dict)
    args: list[SkillArgSpec] = Field(default_factory=list)


class SkillCatalog:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._specs = self._load_specs()
        self._params_summaries: dict[str, str] = {
            name: self._params_summary(spec) for name, spec in self._specs.items()
        }

    def list_available(self) -> list[AvailableSkill]:
        return [
            AvailableSkill(
                name=spec.name,
                summary=spec.summary,
                use_when=spec.use_when,
                params_summary=self._params_summaries[spec.name],
            )
            for spec in self._specs.values()
        ]

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

    @staticmethod
    def _params_summary(spec: SkillSpec) -> str:
        if spec.command_mode == "shell":
            return "command:string(required)"
        if not spec.args:
            return ""
        parts: list[str] = []
        for arg in spec.args:
            arg_type = arg.type
            if arg.repeatable and not arg_type.endswith("_list"):
                arg_type = f"{arg_type}[]"
            flags: list[str] = []
            if arg.required:
                flags.append("required")
            if arg.default not in (None, "", [], {}):
                flags.append(f"default={arg.default}")
            if arg.enum:
                flags.append("enum=" + "|".join(str(item) for item in arg.enum))
            suffix = f"({', '.join(flags)})" if flags else ""
            parts.append(f"{arg.name}:{arg_type}{suffix}")
        return ", ".join(parts)


class SkillExecutor:
    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def run(self, skill_name: str, skill_args: dict[str, Any], *, cwd: Path) -> ExecutionResult:
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.catalog.get(skill_name)
        if spec is None:
            return ExecutionResult(success=False, summary=f"Unknown skill: {skill_name}", stderr="unknown skill")
        binary = self._resolve_bin(spec, skill_args)
        if not shutil.which(binary):
            return ExecutionResult(success=False, summary=f"Skill binary not found: {spec.bin}", stderr="missing binary")
        try:
            command = self._build_command(spec, skill_args, cwd=cwd)
        except ValueError as exc:
            return ExecutionResult(success=False, summary=f"Invalid skill args for {skill_name}", stderr=str(exc))
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, errors="replace")
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

    def _resolve_bin(self, spec: SkillSpec, skill_args: dict[str, Any]) -> str:
        if spec.bin_selector_arg and spec.bin_map:
            selector = str(skill_args.get(spec.bin_selector_arg, "")).strip()
            if selector:
                return spec.bin_map.get(selector, spec.bin)
        return spec.bin

    def _build_command(self, spec: SkillSpec, skill_args: dict[str, Any], *, cwd: Path) -> list[str]:
        if spec.command_mode == "shell":
            command = str((skill_args or {}).get("command", "")).strip()
            if not command:
                raise ValueError("command is required")
            return [spec.bin, "-lc", command]
        if not spec.args:
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

        normalized = self._normalize_args(spec, skill_args or {}, cwd=cwd)
        command = [self._resolve_bin(spec, normalized), *[self._resolve_skill_path(p) for p in spec.base_argv]]
        positional_parts: list[tuple[int, list[str]]] = []
        for arg in spec.args:
            if arg.name not in normalized:
                continue
            value = normalized[arg.name]
            if value is None or value is False:
                continue
            rendered = self._render_arg_value(arg, value)
            if arg.position is not None:
                positional_parts.append((arg.position, rendered))
                continue
            if not arg.flag:
                continue
            if arg.type == "bool":
                if value:
                    command.append(arg.flag)
                continue
            if arg.repeatable and isinstance(value, list):
                for item in value:
                    command.extend([arg.flag, str(item)])
                continue
            command.extend([arg.flag, *rendered])
        for _, parts in sorted(positional_parts, key=lambda item: item[0]):
            command.extend(parts)
        return command

    def _normalize_args(self, spec: SkillSpec, skill_args: dict[str, Any], *, cwd: Path) -> dict[str, Any]:
        known = {arg.name for arg in spec.args}
        unknown = sorted(set(skill_args) - known)
        if unknown:
            raise ValueError(f"unknown args: {', '.join(unknown)}")
        normalized: dict[str, Any] = {}
        for arg in spec.args:
            raw_value = skill_args[arg.name] if arg.name in skill_args else arg.default
            if raw_value is None:
                if arg.required:
                    raise ValueError(f"{arg.name} is required")
                continue
            value = self._coerce_arg_value(arg, raw_value, cwd=cwd)
            if arg.enum and value not in arg.enum:
                raise ValueError(f"{arg.name} must be one of: {', '.join(str(item) for item in arg.enum)}")
            normalized[arg.name] = value
        return normalized

    def _coerce_arg_value(self, arg: SkillArgSpec, value: Any, *, cwd: Path) -> Any:
        if arg.type == "bool":
            return self._coerce_bool(value)
        if arg.type == "int":
            return int(value)
        if arg.type == "string":
            return self._apply_path_policy(arg, str(value), cwd=cwd)
        if arg.type == "string_list":
            items = value if isinstance(value, list) else [value]
            return [self._apply_path_policy(arg, str(item), cwd=cwd) for item in items]
        if arg.type == "int_list":
            items = value if isinstance(value, list) else [value]
            return [int(item) for item in items]
        return value

    def _resolve_skill_path(self, path: str) -> str:
        """Resolve a path relative to skills_dir if it's not absolute."""
        p = Path(path)
        if p.is_absolute():
            return path
        return str((self.catalog.skills_dir / path).resolve())

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        rendered = str(value).strip().lower()
        if rendered in {"1", "true", "yes", "on"}:
            return True
        if rendered in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"invalid bool value: {value}")

    @staticmethod
    def _render_arg_value(arg: SkillArgSpec, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if arg.type == "bool":
            return []
        return [str(value)]

    @staticmethod
    def _apply_path_policy(arg: SkillArgSpec, value: str, *, cwd: Path) -> str:
        if arg.path_policy == "none":
            return value
        # URLs should not be processed as filesystem paths
        if value.startswith(("http://", "https://", "ftp://", "sftp://")):
            return value
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (cwd / candidate).resolve(strict=False)
        base = cwd.resolve(strict=False)
        try:
            relative = resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"{arg.name} escapes work_dir") from exc
        return str(relative) if str(relative) else "."
