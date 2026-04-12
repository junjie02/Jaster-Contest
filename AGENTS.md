# Repository Guidelines

## Project Structure

`src/jaster/` contains the package. `cli.py` exposes the CLI, `runtime/` contains orchestration and platform integration, `agents/` contains prompt-driven JSON agents, `domain/` contains task-tree models, `mcp/` contains MCP client/service code, `storage/` persists runs, and `web/` renders the live task tree.

## Build And Test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

python -m compileall src
pytest
```

## Implementation Notes

- The active runtime is `plan -> parallel strategy -> reflection -> submission`
- Strategy actions are MCP-only
- Do not reintroduce `builder`, `functions/*.json`, or attack-tree node kinds/statuses
- Keep tests under `tests/` using `test_*.py`
- Mock MCP calls and LLM outputs in tests; do not hit real services

## Key Verification Areas

- Task tree patch application
- Strategy round-to-round observation carryover
- Reflection status updates
- CLI and web output consuming `task_tree` instead of legacy attack-tree payloads
