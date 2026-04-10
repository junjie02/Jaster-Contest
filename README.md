# Jaster

Jaster is a clean pentest-agent runtime centered on a shared global attack tree.

## Highlights

- Five agent roles with strict JSON contracts: `recon`, `strategy`, `reflection`, `builder`, `submission`
- Shared attack tree snapshot passed to agents instead of ad-hoc payload blobs
- OpenAI-compatible chat completions client
- File-based run storage
- Direct skill execution and Builder-generated runtime scripts

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
jaster run --target http://example.com
```

## Environment

- `.env` in the project root is loaded automatically by the CLI
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL` optional, defaults to `https://api.openai.com/v1`
- `OPENAI_MODEL` optional, defaults to `gpt-4o-mini`
- `OPENAI_REASONING_SPLIT` optional, must be explicitly set to `true` to enable (no auto-detection); useful for providers such as MiniMax that support `reasoning_split`
- `JASTER_DATA_DIR` optional, defaults to `./data`
- `JASTER_MAX_ROUNDS` optional, defaults to `12`; this is the shared budget for all agent phases (`recon`, `reflection`, `strategy`)
- `JASTER_HTTP_TIMEOUT` optional, defaults to `120`
- `JASTER_LLM_MAX_RETRIES` optional, defaults to `3`; **agent-level** retry — applies only when JSON extraction fails or schema validation fails within a single agent call. Does NOT retry HTTP 5xx errors; use `JASTER_LLM_HTTP_MAX_RETRIES` for that.
- `JASTER_PHASE_MAX_RETRIES` optional, defaults to `3`; applies to same-phase self-correction after an action executes but fails and the agent needs to revise the plan
- `JASTER_LLM_HTTP_MAX_RETRIES` optional, defaults to `3`; HTTP-level retry count for 429/5xx/network errors
- `JASTER_LLM_HTTP_RETRY_BASE_DELAY` optional, defaults to `1.0`; base delay in seconds for exponential backoff
- `JASTER_LLM_HTTP_RETRY_MAX_DELAY` optional, defaults to `8.0`; max delay cap for exponential backoff
- `JASTER_LLM_HTTP_RETRY_JITTER` optional, defaults to `0.2`; jitter added to backoff to avoid thundering herd
- `JASTER_LLM_RATE_LIMIT_MAX_REQUESTS` optional, defaults to `2`; max requests per rate-limit window
- `JASTER_LLM_RATE_LIMIT_WINDOW_SECONDS` optional, defaults to `1.0`; rate-limit window in seconds

使用前修改username和password的存放理解。