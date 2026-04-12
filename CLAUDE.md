# CLAUDE.md

## Project Overview

Jaster is a multi-agent pentest runtime centered on a shared task tree. The active loop is:

```text
plan -> parallel strategy tasks -> reflection -> submission -> repeat
```

There is no builder path and no `functions/*.json` execution path anymore. Strategy actions are MCP-only.

## Commands

```bash
pip install -e .
pip install -e .[dev]

jaster run --target <url>
jaster inspect <run_id>
jaster serve --port 8765

python -m compileall src
pytest
```

## Architecture

### Task Tree

- `TaskTree` lives in `src/jaster/domain/attack_tree.py`
- Every node is a task with `reason`, `completion_criteria`, `attempt_count`, `latest_summary`, and `latest_findings`
- Node statuses are only `in_progress`, `completed`, and `failed`

### Agents

- `PlanAgent`: updates the task tree and dispatches task keys
- `StrategyAgent`: owns one assigned task and can run multiple MCP tools concurrently each round
- `ReflectionAgent`: updates task status and gives planner guidance
- `SubmissionAgent`: filters flag candidates before submission

### MCP

- Root config: `mcp.json`
- MCP service entrypoint: `python -u -m jaster.mcp.mcp_service`
- Sync client wrappers live in `src/jaster/mcp/mcp_client.py`

### Key Files

| File | Purpose |
|------|---------|
| `src/jaster/runtime/orchestrator.py` | Main task-tree orchestration loop |
| `src/jaster/domain/models.py` | Agent contracts and runtime models |
| `src/jaster/domain/attack_tree.py` | Task tree patch / snapshot logic |
| `src/jaster/agents/base.py` | Strict JSON normalization and validation |
| `src/jaster/mcp/mcp_service.py` | Migrated MCP tools |
| `src/jaster/web/app.js` | Task tree viewer |

## Testing Focus

- Task tree patching
- Agent response normalization
- Orchestrator cycle semantics
- MCP wrapper behavior with mocked tool inventory / tool calls
