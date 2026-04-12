from __future__ import annotations

import json
import uuid
from pathlib import Path

from jaster.domain import ArtifactRef, RunState
from jaster.runtime.artifacts import filter_available_artifacts


class FileRunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, state: RunState) -> RunState:
        run_dir = self.run_dir(state.run_id)
        (run_dir / "rounds").mkdir(parents=True, exist_ok=True)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        self.save_state(state)
        return state

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def save_state(self, state: RunState) -> None:
        run_dir = self.run_dir(state.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": state.run_id,
                    "challenge": state.challenge.model_dump(),
                    "planner_context": state.planner_context.model_dump() if state.planner_context else None,
                    "available_artifacts": [item.model_dump() for item in filter_available_artifacts(state.available_artifacts)],
                    "planner_history": [item.model_dump() for item in state.planner_history],
                    "reflection_history": [item.model_dump() for item in state.reflection_history],
                    "latest_discoveries": [item.model_dump() for item in state.latest_discoveries],
                    "submitted_flags": state.submitted_flags,
                    "rounds_completed": state.rounds_completed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (run_dir / "tree.json").write_text(
            json.dumps(state.task_tree.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "observations.jsonl").write_text(
            "\n".join(json.dumps(item.model_dump(), ensure_ascii=False) for item in state.observations),
            encoding="utf-8",
        )

    def append_round(self, run_id: str, name: str, payload: dict) -> None:
        path = self.run_dir(run_id) / "rounds" / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, run_id: str) -> RunState:
        run_dir = self.run_dir(run_id)
        run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        tree_payload = json.loads((run_dir / "tree.json").read_text(encoding="utf-8"))
        observations = []
        obs_path = run_dir / "observations.jsonl"
        if obs_path.exists():
            observations = [
                _normalize_observation_payload(json.loads(line))
                for line in obs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        raw_artifacts = [
            ArtifactRef.model_validate(item) for item in run_payload.get("available_artifacts", [])
        ]
        return RunState.model_validate(
            {
                **run_payload,
                "available_artifacts": [item.model_dump() for item in filter_available_artifacts(raw_artifacts)],
                "task_tree": tree_payload,
                "observations": observations,
            }
        )

def _normalize_observation_payload(payload: dict) -> dict:
    normalized = dict(payload or {})
    normalized["cycle"] = int(normalized.get("cycle") or 0)
    normalized["strategy_round"] = int(normalized.get("strategy_round") or 0)
    normalized["task_key"] = str(normalized.get("task_key") or "")
    normalized["task_title"] = str(normalized.get("task_title") or "")
    normalized["action_task_id"] = str(normalized.get("action_task_id") or normalized.get("task_id") or "")
    normalized["tool_name"] = str(normalized.get("tool_name") or "")
    normalized["target"] = str(normalized.get("target") or "")
    normalized["result"] = str(normalized.get("result") or normalized.get("summary") or "")
    normalized["key_findings"] = str(normalized.get("key_findings") or "")
    return normalized
