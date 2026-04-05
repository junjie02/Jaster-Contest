from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import typer

from jaster.domain import ChallengeSpec
from jaster.runtime.env import env_int, load_dotenv
from jaster.runtime.llm import OpenAIChatClient
from jaster.runtime.orchestrator import JasterOrchestrator, detect_target_type, detect_zone
from jaster.runtime.server import SSEBroadcaster, start_server
from jaster.storage.files import FileRunStore

app = typer.Typer(help="Jaster pentest agent runtime.")

_broadcaster: SSEBroadcaster | None = None
_server_url: str | None = None


def _get_broadcaster() -> SSEBroadcaster:
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = SSEBroadcaster()
    return _broadcaster


def _get_server_url() -> str | None:
    return _server_url


def _set_server_url(url: str) -> None:
    global _server_url
    _server_url = url


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


load_dotenv(_project_root() / ".env")


def _data_dir(root: Path) -> Path:
    configured = os.environ.get("JASTER_DATA_DIR", "data").strip() or "data"
    path = Path(configured)
    return path if path.is_absolute() else root / path


@app.command()
def run(
    target: str = typer.Option(...),
    description: str = typer.Option("", help="Challenge description"),
    zone: str = typer.Option("", help="Zone override"),
    target_type: str = typer.Option("", help="http or tcp"),
    max_recon_steps: int = typer.Option(env_int("JASTER_MAX_RECON_STEPS", 3)),
    max_rounds: int = typer.Option(env_int("JASTER_MAX_ROUNDS", 12)),
) -> None:
    root = _project_root()
    challenge = ChallengeSpec(
        target=target,
        target_type=target_type or detect_target_type(target),
        description=description,
        zone=zone or detect_zone(description),
    )

    def _post_tree_update(snapshot: object) -> None:
        url = _get_server_url()
        if not url:
            return
        try:
            import httpx
            httpx.post(f"{url}/tree_update", json=snapshot, timeout=5)
        except Exception:
            pass  # server not running, silently ignore

    orchestrator = JasterOrchestrator(
        store=FileRunStore(_data_dir(root) / "runs"),
        prompt_root=root / "src" / "jaster" / "prompts",
        skills_dir=root / "skills",
        llm=OpenAIChatClient(),
        on_tree_update=_post_tree_update,
    )
    state = orchestrator.run(challenge, max_recon_steps=max_recon_steps, max_rounds=max_rounds)
    typer.echo(json.dumps(state.model_dump(), ensure_ascii=False, indent=2))


@app.command()
def inspect(run_id: str) -> None:
    root = _project_root()
    store = FileRunStore(_data_dir(root) / "runs")
    state = store.load(run_id)
    typer.echo(json.dumps(state.model_dump(), ensure_ascii=False, indent=2))


@app.command()
def serve(
    host: str = "0.0.0.0",
    port: int = 8765,
) -> None:
    broadcaster = _get_broadcaster()
    _set_server_url(f"http://{host}:{port}")
    data_root = _data_dir(_project_root()) / "runs"
    thread = threading.Thread(target=start_server, args=(broadcaster, host, port, data_root), daemon=True)
    thread.start()
    print(f"[*] SSE server running on http://{host}:{port}", flush=True)
    print(f"[*] Press Ctrl+C to stop", flush=True)
    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n[*] Server stopped")


if __name__ == "__main__":
    app()
