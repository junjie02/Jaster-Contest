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
- `OPENAI_REASONING_SPLIT` optional, useful for providers such as MiniMax that support `reasoning_split`
- `JASTER_DATA_DIR` optional, defaults to `./data`
- `JASTER_MAX_RECON_STEPS` optional, defaults to `3`
- `JASTER_MAX_ROUNDS` optional, defaults to `12`
- `JASTER_HTTP_TIMEOUT` optional, defaults to `120`
- `JASTER_LLM_MAX_RETRIES` optional, defaults to `3`
