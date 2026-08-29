# Agent backend — :8002

The docs for this service moved to the repo root in the phase 3 reorg. Nothing was deleted.

| Doc | Now at |
|---|---|
| Architecture + invariants (long, authoritative — read this first) | [../../docs/agent/CLAUDE.md](../../docs/agent/CLAUDE.md) |
| Staged build order | [../../docs/agent/BUILD_PLAN.md](../../docs/agent/BUILD_PLAN.md) |
| What exists, what doesn't, bugs found by running it | [../../docs/agent/PROGRESS.md](../../docs/agent/PROGRESS.md) |
| Ingestion and retrieval detail | [../../docs/agent/INGESTION_AND_RETRIEVAL.md](../../docs/agent/INGESTION_AND_RETRIEVAL.md) |
| Cross-service request/response shapes | [../../docs/CONTRACTS.md](../../docs/CONTRACTS.md) |

```bash
uv sync
uv run uvicorn app.main:app --port 8002    # ONE worker, no --reload
uv run pytest -q --tb=line                 # 312 tests, no API key, no network
```

`SessionStore` is in-process: a second worker or a reload mid-conversation loses live carts.
