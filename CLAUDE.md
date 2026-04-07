# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Jaster is a multi-agent penetration testing runtime centered on a shared global attack tree. Five specialized agents (recon, strategy, reflection, builder, submission) communicate via strict JSON contracts, orchestrated around an OpenAI-compatible LLM client.

## Commands

```bash
pip install -e .          # Base install
pip install -e .[dev]      # With dev dependencies (includes pytest)

jaster run --target <url>  # Run a pentest
jaster inspect <run_id>     # Inspect a past run
jaster serve --port 8765   # Start SSE server for web UI

pytest                      # Run all tests
pytest tests/test_foo.py    # Run a single test file
```

## Architecture

### Orchestration Loop
`JasterOrchestrator.run()` executes up to `JASTER_MAX_ROUNDS` (default 12) iterations through three phases:
```
recon → reflection → strategy → (repeat or finish)
```

- **Recon agent**: Explores target, expands attack tree with nodes (assets, weaknesses, techniques), picks action
- **Reflection agent**: Organizes findings, provides strategic guidance, updates `next_focus_key`
- **Strategy agent**: Targets specific exploitable node, formulates exploitation plan
- **Builder agent**: On-demand LLM generates Python/shell scripts for complex tasks
- **Submission agent**: Evaluates flag candidates and decides whether to submit

### Attack Tree
Central data structure shared across all agents. Agents return `TreePatch` objects (add/update nodes) applied to the tree. Node kinds: `target`, `asset`, `entry`, `weakness`, `technique`, `hypothesis`. Statuses: `unexplored`, `exploring`, `success`, `failed`.

### Skills System
Skill definitions are JSON specs in `skills/*.json` (port_scan, sqli_exploit, ffuf_bruteforce, etc.). `SkillExecutor` runs them as subprocesses with proper argument handling. Agents choose between `skill`, `builder`, or `finish` actions.

### Zone System
4 zones with different focus areas, determined by `detect_zone()`:
- **zone1**: Default (general pentest)
- **zone2**: CVE/cloud focus
- **zone3**: Pivot/multi-step focus
- **zone4**: Kerberos/AD focus

Prompt templates: `prompts/shared.md` + `prompts/agents/{role}.md` + `prompts/zones/{zone}.md`

### Agent JSON Contracts
Each agent has strict input/output Pydantic models (defined in `src/jaster/domain/models.py`): `ReconInput→ReconOutput`, `StrategyInput→StrategyOutput`, etc. `JsonAgent` base class handles LLM communication, JSON parsing with retry logic, and response normalization.

## Key Files

| File | Purpose |
|------|---------|
| `src/jaster/cli.py` | CLI entry point (Typer) |
| `src/jaster/runtime/orchestrator.py` | Main orchestration brain |
| `src/jaster/runtime/skills.py` | Skill catalog and execution |
| `src/jaster/runtime/llm.py` | OpenAI-compatible LLM client |
| `src/jaster/domain/attack_tree.py` | Attack tree data structure |
| `src/jaster/domain/models.py` | All Pydantic contract models |
| `src/jaster/runtime/prompts.py` | Prompt template rendering |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | Required | API authentication |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name |
| `JASTER_DATA_DIR` | `./data` | Run storage path |
| `JASTER_MAX_ROUNDS` | `12` | Phase iteration budget |
| `JASTER_LLM_MAX_RETRIES` | `3` | Retries per agent call |
| `JASTER_PHASE_MAX_RETRIES` | `3` | Phase-level self-correction retries |
| `JASTER_HTTP_TIMEOUT` | `120` | HTTP request timeout |
