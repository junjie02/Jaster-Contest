from __future__ import annotations

import json
import uuid
from pathlib import Path

from jaster.domain import RunState


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
                    "submitted_flags": state.submitted_flags,
                    "rounds_completed": state.rounds_completed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (run_dir / "tree.json").write_text(
            json.dumps(state.tree.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "observations.jsonl").write_text(
            "\n".join(json.dumps(item.model_dump(), ensure_ascii=False) for item in state.observations),
            encoding="utf-8",
        )

    def append_round(self, run_id: str, index: int, payload: dict) -> None:
        path = self.run_dir(run_id) / "rounds" / f"{index:03d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_agent_round(self, run_id: str, agent_type: str, round_num: int, payload: dict) -> None:
        """保存 agent 独立的 round 日志，文件名格式: {agent}_round_{n}.json"""
        path = self.run_dir(run_id) / "rounds" / f"{agent_type}_round_{round_num}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, run_id: str) -> RunState:
        run_dir = self.run_dir(run_id)
        run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        tree_payload = json.loads((run_dir / "tree.json").read_text(encoding="utf-8"))
        observations = []
        obs_path = run_dir / "observations.jsonl"
        if obs_path.exists():
            observations = [
                json.loads(line) for line in obs_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        return RunState.model_validate(
            {
                **run_payload,
                "tree": tree_payload,
                "observations": observations,
            }
        )

