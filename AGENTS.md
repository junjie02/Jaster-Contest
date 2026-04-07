# Repository Guidelines

## Project Structure & Module Organization
`src/jaster/` contains the Python package. `cli.py` exposes the `jaster` entrypoint, `runtime/` handles orchestration and LLM integration, `agents/` defines role behavior, `domain/` holds shared models such as the attack tree, and `storage/` manages file-backed persistence. Prompt templates live in `src/jaster/prompts/`, the lightweight web UI is in `src/jaster/web/`, and reusable action definitions are stored as JSON files under `skills/`.

## Build, Test, and Development Commands
Create a local environment and install the package with dev tools:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Run the CLI locally with `jaster run --target http://example.com`. Use `python -m pytest` to run tests once a `tests/` suite exists. For quick package validation, `python -m compileall src` catches basic syntax issues without external services.

## Coding Style & Naming Conventions
Target Python 3.11+ and keep modules focused by responsibility. Follow PEP 8: 4-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and descriptive field names for Pydantic models. Keep prompt files and skill JSON names lowercase with underscores, matching existing examples like `web_content_discovery.json`. Prefer small, typed functions and avoid mixing CLI, runtime, and domain concerns in one file.

## Testing Guidelines
`pytest` is configured in [pyproject.toml](/home/painting/桌面/Jaster/pyproject.toml), with `src` on `PYTHONPATH` and `tests/` as the expected test root. Add new tests under `tests/` using `test_*.py` filenames. Prioritize coverage for attack-tree updates, runtime orchestration, and skill execution. When networked providers are involved, mock the HTTP layer rather than calling real endpoints.

## Commit & Pull Request Guidelines
Recent history uses short, imperative commit messages, often in Chinese, for example `修复reflection 巡航bug` and `实现密码爆破`. Keep commits focused on one change. Pull requests should explain the behavior change, list affected modules, note any new environment variables or skills, and include CLI output or screenshots when `src/jaster/web/` is touched.

## Security & Configuration Tips
Do not commit real credentials, target data, or populated `.env` files. Keep `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, and related runtime settings in local environment configuration only. Treat files under `skills/docs/` and generated run data as sensitive inputs/outputs.
