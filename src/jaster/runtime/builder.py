from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jaster.domain import ArtifactRef, BuilderOutput, ExecutionResult, Observation
from jaster.runtime.json_extract import extract_json_object


class BuilderExecutor:
    def run(
        self,
        builder_output: BuilderOutput,
        *,
        target: str,
        target_type: str,
        working_dir: Path,
        accessible_artifacts: list[ArtifactRef],
        recent_observations: list[Observation],
        latest_execution: ExecutionResult | None,
    ) -> ExecutionResult:
        working_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "builder_tool.py"
        script_path.write_text(builder_output.script, encoding="utf-8")
        input_payload = {
            "target": target,
            "target_type": target_type,
            "working_dir": str(working_dir),
            "accessible_artifacts": [item.model_dump() for item in accessible_artifacts],
            "recent_observations": [item.model_dump() for item in recent_observations],
            "latest_execution": latest_execution.model_dump() if latest_execution is not None else None,
        }
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps(input_payload),
            capture_output=True,
            text=True,
            errors="replace",
            cwd=working_dir,
        )
        try:
            output = extract_json_object(completed.stdout) if completed.stdout.strip() else {}
        except ValueError:
            output = {}
        output_artifacts = [
            ArtifactRef.model_validate(item)
            for item in output.get("artifacts", [])
            if isinstance(item, dict)
        ]
        return ExecutionResult(
            success=completed.returncode == 0,
            summary=str(output.get("summary") or builder_output.summary),
            findings=[str(item) for item in output.get("findings", []) if str(item).strip()],
            flag_candidates=[str(item) for item in output.get("flag_candidates", []) if str(item).strip()],
            artifacts=[
                ArtifactRef(kind="work_dir", path=str(working_dir)),
                ArtifactRef(kind="script", path=str(script_path)),
                *output_artifacts,
            ],
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            command=f"{sys.executable} {script_path}",
            script_path=str(script_path),
        )
