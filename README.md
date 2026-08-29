# toes — conversational commerce stack

A shopper talks to an AI agent that helps them **discover → decide → pay** in one
conversation. A merchant uploads a spreadsheet; the agent derives what the columns mean and
shops the catalog on the shopper's behalf.

For the repo map read [CLAUDE.md](CLAUDE.md), the architecture in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the cross-service shapes in
**[docs/CONTRACTS.md](docs/CONTRACTS.md)** — the source of truth for every seam. For the work in flight read
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and [PROGRESS_LOG.md](PROGRESS_LOG.md).

---

## Services and ports

Ports are fixed repo-wide. Use these and nothing else.

| Service | Directory | Entrypoint | Port |
|---|---|---|---|
| Merchant backend | `services/merchant/` | `app.main:app` | 8001 |
| Agent backend | `services/agent/` | `app.main:app` | 8002 |
| Payments | `services/payments/` | `app.main:app` | 8003 |
| Stubs (merchant + payment, for agent dev) | `services/agent/stubs/` | `stubs.mock_services:app` | 9001 |
| Merchant console | `web/merchant-console/` | `index.html` | static |
| Storefront | `web/storefront/` | `chatbot.html` | static |

---

## Running things

### Merchant backend

Imports are `app.*` and resolve with `services/merchant/` as the working directory
(`pyproject.toml` sets `pythonpath = ["."]`).

```bash
cd services/merchant
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8001
```

Storage is in-memory unless `DATABASE_URL` is set — the catalog does not survive a restart.

### Agent backend

```bash
cd services/agent
uv sync
uv run uvicorn app.main:app --port 8002       # ONE worker, no --reload
```

`SessionStore` is in-process. A second worker or a reload mid-conversation loses live carts.

### The stubs the agent was built against

```bash
cd services/agent
uv run uvicorn stubs.mock_services:app --port 9001
```

Serves both the merchant and payment contracts. `MERCHANT_BASE_URL` and `PAYMENT_BASE_URL`
default here.

### Payments

```bash
docker compose up -d db          # from the repo root
cd services/payments
uv sync
uv run uvicorn app.main:app --port 8003
```

Postgres is required — `init_pool()` runs in the lifespan, so the service will not start
without it.

---

## Tests

```bash
cd services/merchant && python -m pytest -q          # 43 tests, no infra
cd services/agent    && uv run pytest -q --tb=line   # 312 tests, no API key, no network
cd services/payments && uv run pytest -q            # 7 tests, needs Postgres
```

The agent suite is the safety net for the whole repo: it runs in ~28s offline, and a drop in
the pass count is a regression even when nothing errors.

---

## API — merchant backend

- `POST /catalog/upload` — upload a merchant CSV (multipart: `merchant_id`, `file`)
- `GET  /catalog?merchant_id=` — list a merchant's normalized products
- `GET  /catalog/search?merchant_id=&query=&category=&min_price=&max_price=&in_stock_only=`
- `GET  /categories?merchant_id=`
- `PATCH /catalog/{product_id}?merchant_id=`
- `GET  /health`

Interactive docs at `http://127.0.0.1:8001/docs`.

**Normalization is for the merchant's own console display only.** Required fields are `id`,
`title`, `price`; missing stock defaults to `1` with a warning, a missing image warns without
blocking. The agent does not consume normalized products — it needs the raw sheet, because
its whole premise is that column layout is arbitrary and derived. That boundary is being
formalized in phase 4 of the implementation plan.

---

## Known breakage

Verified, not assumed — see the "Known breakage" section of [CLAUDE.md](CLAUDE.md) and the shapes in
[docs/CONTRACTS.md](docs/CONTRACTS.md) before
debugging anything cross-service. The short version: the merchant and payment HTTP contracts
do not currently match what the agent's clients send, because the agent was built against the
stubs rather than the real services.
