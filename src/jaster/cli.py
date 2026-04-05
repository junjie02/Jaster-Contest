from __future__ import annotations

import json
from pathlib import Path

import typer

from jaster.domain import ChallengeSpec
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.orchestrator import JasterOrchestrator, detect_target_type, detect_zone
from jaster.storage.files import FileRunStore

app = typer.Typer(help="Jaster pentest agent runtime.")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@app.command()
def run(
    target: str = typer.Option(...),
    description: str = typer.Option("", help="Challenge description"),
    zone: str = typer.Option("", help="Zone override"),
    target_type: str = typer.Option("", help="http or tcp"),
    max_recon_steps: int = typer.Option(3),
    max_rounds: int = typer.Option(12),
) -> None:
    root = _project_root()
    challenge = ChallengeSpec(
        target=target,
        target_type=target_type or detect_target_type(target),
        description=description,
        zone=zone or detect_zone(description),
    )
    orchestrator = JasterOrchestrator(
        store=FileRunStore(root / "data" / "runs"),
        prompt_root=root / "src" / "jaster" / "prompts",
        skills_dir=root / "skills",
        llm=OpenAIChatClient(),
    )
    state = orchestrator.run(challenge, max_recon_steps=max_recon_steps, max_rounds=max_rounds)
    typer.echo(json.dumps(state.model_dump(), ensure_ascii=False, indent=2))


@app.command()
def inspect(run_id: str) -> None:
    root = _project_root()
    store = FileRunStore(root / "data" / "runs")
    state = store.load(run_id)
    typer.echo(json.dumps(state.model_dump(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()

