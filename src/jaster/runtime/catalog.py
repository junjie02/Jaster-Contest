from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from jaster.domain import ArtifactRef, AvailableFunction, AvailableSkill, ExecutionResult


class FunctionArgSpec(BaseModel):
    name: str
    type: Literal["string", "int", "bool", "string_list", "int_list"] = "string"
    flag: str = ""
    position: int | None = None
    repeatable: bool = False
    required: bool = False
    default: Any = None
    enum: list[Any] = Field(default_factory=list)
    path_policy: Literal["none", "work_dir_relative", "work_dir_output", "repo_relative"] = "none"


class FunctionSpec(AvailableSkill):
    command_mode: Literal["argv", "shell"] = "argv"
    bin: str = ""
    base_argv: list[str] = Field(default_factory=list)
    primary_locator_arg: str = ""
    shape_signature_args: list[str] = Field(default_factory=list)
    variant_signature_args: list[str] = Field(default_factory=list)
    bin_selector_arg: str = ""
    bin_map: dict[str, str] = Field(default_factory=dict)
    args: list[FunctionArgSpec] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)


class SkillDoc(AvailableSkill):
    body: str = ""


class RuntimeCatalog:
    def __init__(self, functions_dir: Path, skills_dir: Path) -> None:
        self.functions_dir = functions_dir
        self.skills_dir = skills_dir
        self._function_definition_texts: dict[str, str] = {}
        self._function_specs = self._load_function_specs()
        self._skill_docs = self._load_skill_docs()

    def list_functions(self) -> list[AvailableFunction]:
        return [
            AvailableFunction(
                name=spec.name,
                summary=spec.summary,
                use_when=spec.use_when,
            )
            for spec in self._function_specs.values()
        ]

    def list_skills(self) -> list[AvailableSkill]:
        return [
            AvailableSkill(name=skill.name, summary=skill.summary, use_when=skill.use_when)
            for skill in self._skill_docs.values()
        ]

    def get_function(self, name: str) -> FunctionSpec | None:
        return self._function_specs.get(name)

    def get_function_definition_text(self, name: str) -> str:
        return self._function_definition_texts.get(name, "")

    def get_skill(self, name: str) -> SkillDoc | None:
        return self._skill_docs.get(name)

    def _load_function_specs(self) -> dict[str, FunctionSpec]:
        specs: dict[str, FunctionSpec] = {}
        if not self.functions_dir.exists():
            return specs
        for path in sorted(self.functions_dir.glob("*.json")):
            raw_text = path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
            spec = FunctionSpec.model_validate(payload)
            specs[spec.name] = spec
            self._function_definition_texts[spec.name] = raw_text
        return specs

    def _load_skill_docs(self) -> dict[str, SkillDoc]:
        docs: dict[str, SkillDoc] = {}
        if not self.skills_dir.exists():
            return docs
        for path in sorted(self.skills_dir.glob("*.md")):
            try:
                skill = self._parse_skill_doc(path)
            except Exception:
                skill = SkillDoc(name=path.stem, body=path.read_text(encoding="utf-8"))
            docs[skill.name] = skill
        return docs

    def _parse_skill_doc(self, path: Path) -> SkillDoc:
        text = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = text
        if text.startswith("---\n"):
            parts = text.split("---\n", 2)
            if len(parts) == 3:
                _, frontmatter, remainder = parts
                body = remainder.strip()
                for line in frontmatter.splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
        return SkillDoc(
            name=meta.get("name") or path.stem,
            summary=meta.get("summary", ""),
            use_when=meta.get("use_when", ""),
            body=body,
        )

    def render_inspiration(self, selected_skills: list[str]) -> str:
        parts: list[str] = []
        labels = ["Primary Skill", "Secondary Skill"]
        for index, name in enumerate(selected_skills[:2]):
            skill = self.get_skill(name)
            if skill is None:
                continue
            block = [
                f"{labels[index]}: {skill.name}",
                f"Summary: {skill.summary}",
            ]
            if skill.use_when:
                block.append(f"Use When: {skill.use_when}")
            if skill.body:
                block.append(skill.body)
            parts.append("\n".join(block))
        return "\n\n".join(parts).strip()

    def build_tool(self, name: str) -> dict[str, Any]:
        spec = self.get_function(name)
        if spec is None:
            raise KeyError(name)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in spec.args:
            schema = self._arg_json_schema(arg)
            properties[arg.name] = schema
            if arg.required:
                required.append(arg.name)
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": self._tool_description(spec),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def tool_prompt_text(self, name: str) -> str:
        spec = self.get_function(name)
        if spec is None:
            return ""
        lines = [
            f"name: {spec.name}",
            f"summary: {spec.summary}",
        ]
        if spec.use_when:
            lines.append(f"use_when: {spec.use_when}")
        if spec.args:
            lines.append("params:")
            for arg in spec.args:
                flags: list[str] = [arg.type]
                if arg.required:
                    flags.append("required")
                if arg.enum:
                    flags.append("enum=" + "|".join(str(item) for item in arg.enum))
                if arg.default not in (None, "", [], {}):
                    flags.append(f"default={arg.default}")
                if arg.path_policy != "none":
                    flags.append(f"path_policy={arg.path_policy}")
                lines.append(f"- {arg.name}: {', '.join(flags)}")
        return "\n".join(lines)

    @staticmethod
    def _tool_description(spec: FunctionSpec) -> str:
        parts = [spec.summary]
        if spec.use_when:
            parts.append(f"use_when: {spec.use_when}")
        return " ".join(part for part in parts if part).strip()

    @staticmethod
    def _arg_json_schema(arg: FunctionArgSpec) -> dict[str, Any]:
        if arg.type == "int":
            schema: dict[str, Any] = {"type": "integer"}
        elif arg.type == "bool":
            schema = {"type": "boolean"}
        elif arg.type == "string_list":
            schema = {"type": "array", "items": {"type": "string"}}
        elif arg.type == "int_list":
            schema = {"type": "array", "items": {"type": "integer"}}
        else:
            schema = {"type": "string"}
        desc_bits: list[str] = []
        if arg.flag:
            desc_bits.append(f"flag={arg.flag}")
        if arg.position is not None:
            desc_bits.append(f"position={arg.position}")
        if arg.path_policy != "none":
            desc_bits.append(f"path_policy={arg.path_policy}")
        if arg.repeatable:
            desc_bits.append("repeatable")
        if desc_bits:
            schema["description"] = ", ".join(desc_bits)
        if arg.enum:
            schema["enum"] = arg.enum
        return schema


class FunctionExecutor:
    def __init__(self, catalog: RuntimeCatalog) -> None:
        self.catalog = catalog

    def run(self, function_name: str, function_args: dict[str, Any], *, cwd: Path) -> ExecutionResult:
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.catalog.get_function(function_name)
        if spec is None:
            return ExecutionResult(success=False, summary=f"Unknown function: {function_name}", stderr="unknown function")
        binary = self._resolve_bin(spec, function_args)
        if not shutil.which(binary):
            return ExecutionResult(success=False, summary=f"Function binary not found: {spec.bin}", stderr="missing binary")
        try:
            normalized_args = self._normalize_args(spec, function_args or {}, cwd=cwd) if spec.args else dict(function_args or {})
            command = self._build_command_from_normalized(spec, normalized_args)
        except ValueError as exc:
            return ExecutionResult(
                success=False,
                summary=f"Invalid function args for {function_name}",
                stderr=str(exc),
                failure_stage="function_execution",
            )
        before_paths = snapshot_work_dir(cwd)
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, errors="replace")
        artifacts = discover_artifacts(
            cwd,
            before_paths=before_paths,
            declared_outputs=self._declared_output_paths(spec, normalized_args, cwd=cwd),
        )
        summary = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        return ExecutionResult(
            success=completed.returncode == 0,
            summary=summary or f"Function {function_name} finished",
            findings=[line for line in completed.stdout.splitlines() if line.strip()][:10],
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            command=shlex.join(command),
            artifacts=artifacts,
            failure_stage="" if completed.returncode == 0 else "function_execution",
        )

    def run_function(self, function_name: str, function_args: dict[str, Any], *, cwd: Path) -> ExecutionResult:
        return self.run(function_name, function_args, cwd=cwd)

    def _resolve_bin(self, spec: FunctionSpec, function_args: dict[str, Any]) -> str:
        if spec.bin_selector_arg and spec.bin_map:
            selector = str(function_args.get(spec.bin_selector_arg, "")).strip()
            if selector:
                return spec.bin_map.get(selector, spec.bin)
        return spec.bin

    def _build_command(self, spec: FunctionSpec, function_args: dict[str, Any], *, cwd: Path) -> list[str]:
        normalized = self._normalize_args(spec, function_args or {}, cwd=cwd)
        return self._build_command_from_normalized(spec, normalized)

    def _build_command_from_normalized(self, spec: FunctionSpec, normalized: dict[str, Any]) -> list[str]:
        if spec.command_mode == "shell":
            command = str((normalized or {}).get("command", "")).strip()
            if not command:
                raise ValueError("command is required")
            return [spec.bin, "-lc", command]
        if not spec.args:
            command = [spec.bin]
            for key, value in normalized.items():
                if value is None or value is False:
                    continue
                flag = f"--{key.replace('_', '-')}"
                if value is True:
                    command.append(flag)
                else:
                    command.extend([flag, str(value)])
            return command

        command = [self._resolve_bin(spec, normalized), *spec.base_argv]
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

    def _declared_output_paths(self, spec: FunctionSpec, normalized_args: dict[str, Any], *, cwd: Path) -> list[Path]:
        outputs: list[Path] = []
        for arg in spec.args:
            if arg.path_policy != "work_dir_output" or arg.name not in normalized_args:
                continue
            value = normalized_args[arg.name]
            if isinstance(value, list):
                candidates = value
            else:
                candidates = [value]
            for candidate in candidates:
                path = Path(str(candidate))
                resolved = path if path.is_absolute() else (cwd / path).resolve(strict=False)
                if resolved.exists():
                    outputs.append(resolved)
        return outputs

    def _normalize_args(self, spec: FunctionSpec, function_args: dict[str, Any], *, cwd: Path) -> dict[str, Any]:
        known = {arg.name for arg in spec.args}
        unknown = sorted(set(function_args) - known)
        if unknown:
            raise ValueError(f"unknown args: {', '.join(unknown)}")
        normalized: dict[str, Any] = {}
        for arg in spec.args:
            raw_value = function_args[arg.name] if arg.name in function_args else arg.default
            if raw_value is None:
                if arg.required:
                    raise ValueError(f"{arg.name} is required")
                continue
            value = self._coerce_arg_value(arg, raw_value, cwd=cwd)
            if arg.enum and value not in arg.enum:
                raise ValueError(f"{arg.name} must be one of: {', '.join(str(item) for item in arg.enum)}")
            normalized[arg.name] = value
        return normalized

    def _coerce_arg_value(self, arg: FunctionArgSpec, value: Any, *, cwd: Path) -> Any:
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

    def _resolve_repo_path(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            return str(p.resolve())
        return str((self.catalog.functions_dir.parent / p).resolve())

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
    def _render_arg_value(arg: FunctionArgSpec, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if arg.type == "bool":
            return []
        return [str(value)]

    def _apply_path_policy(self, arg: FunctionArgSpec, value: str, *, cwd: Path) -> str:
        if arg.path_policy == "none":
            return value
        # URLs should not be processed as filesystem paths
        if value.startswith(("http://", "https://", "ftp://", "sftp://")):
            return value
        if arg.path_policy == "repo_relative":
            return self._resolve_repo_path(value)
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


def snapshot_work_dir(cwd: Path) -> set[str]:
    if not cwd.exists():
        return set()
    snapshots: set[str] = set()
    for path in cwd.rglob("*"):
        try:
            relative = path.relative_to(cwd)
        except ValueError:
            continue
        rendered = str(relative)
        if rendered:
            snapshots.add(rendered)
    return snapshots


def discover_artifacts(cwd: Path, *, before_paths: set[str], declared_outputs: list[Path] | None = None) -> list[ArtifactRef]:
    artifacts: list[ArtifactRef] = []
    declared_outputs = declared_outputs or []
    include_root_dir = False
    for path in declared_outputs:
        resolved = path.resolve(strict=False)
        if resolved == cwd.resolve(strict=False):
            include_root_dir = True
            continue
        artifact = _artifact_for_path(path)
        if artifact is not None:
            artifacts.append(artifact)

    after_paths = snapshot_work_dir(cwd)
    new_paths = sorted(after_paths - before_paths, key=lambda item: (item.count("/"), item))
    if include_root_dir:
        root_artifact = _artifact_for_path(cwd)
        if root_artifact is not None:
            artifacts.append(root_artifact)
            return _dedupe_artifacts(artifacts)

    selected_dirs: list[str] = []
    for relative in new_paths:
        if any(relative == parent or relative.startswith(parent + "/") for parent in selected_dirs):
            continue
        absolute = (cwd / relative).resolve(strict=False)
        if not absolute.exists():
            continue
        artifact = _artifact_for_path(absolute)
        if artifact is None:
            continue
        artifacts.append(artifact)
        if absolute.is_dir():
            selected_dirs.append(relative)
    return _dedupe_artifacts(artifacts)


def filter_available_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    filtered: list[ArtifactRef] = []
    for artifact in artifacts:
        if artifact.kind not in {"file", "directory"}:
            continue
        path = Path(artifact.path)
        if artifact.kind == "directory":
            if not _is_non_empty_dir(path):
                continue
        elif not _is_non_empty_file(path):
            continue
        filtered.append(artifact)
    return _dedupe_artifacts(filtered)


def _artifact_for_path(path: Path) -> ArtifactRef | None:
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        return None
    if resolved.is_dir():
        if not _is_non_empty_dir(resolved):
            return None
        return ArtifactRef(kind="directory", path=str(resolved))
    if resolved.is_file():
        if not _is_non_empty_file(resolved):
            return None
        return ArtifactRef(kind="file", path=str(resolved))
    return None


def _is_non_empty_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except (StopIteration, OSError):
        return False
    return True


def _is_non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _dedupe_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    deduped: list[ArtifactRef] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        key = (artifact.kind, artifact.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped
