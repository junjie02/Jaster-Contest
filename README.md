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
set -a
source .env
set +a
jaster run --target http://example.com
```

## Environment

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL` optional, defaults to `https://api.openai.com/v1`
- `OPENAI_MODEL` optional, defaults to `gpt-4o-mini`
