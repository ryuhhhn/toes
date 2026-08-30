# toes — conversational commerce

**A shopper talks to an AI agent that helps them discover → decide → pay, in one
conversation. A merchant uploads a spreadsheet; the agent works out what the columns mean
and shops that catalog on the shopper's behalf.**

No category is hardcoded. The same code sells power tools, loose-leaf tea and eyewear,
because the meaning of a catalog is *derived* from the sheet and then *approved* by the
merchant — never assumed by the code.

```
merchant uploads any spreadsheet ──▶ agent derives what each column means
                                     merchant approves it
                                          │
shopper: "something for my dad's shed, under £150"
                                          ▼
      retrieve ──▶ ask the 1–2 questions that matter for THIS catalog
               ──▶ compare on the axes they care about
               ──▶ preview ──▶ explicit consent ──▶ charge ──▶ receipt
```

---

## Table of contents

- [What makes it different](#what-makes-it-different)
- [Quick start](#quick-start)
- [Try it end to end](#try-it-end-to-end)
- [Architecture](#architecture)
- [Repo layout](#repo-layout)
- [Configuration](#configuration)
- [Tests](#tests)
- [API surface](#api-surface)
- [Design rules that are structural, not stylistic](#design-rules-that-are-structural-not-stylistic)
- [Operational gotchas](#operational-gotchas)
- [Documentation](#documentation)

---

## What makes it different

**The catalog teaches the agent, not the other way round.** Most shopping bots need a
schema: a `price` field, a `category` enum, a taxonomy someone maintained. Here the merchant
uploads whatever they already have — `RRP inc VAT`, `Lens_Width_mm`, `stock on hand` — and an
ingestion pipeline profiles the columns, clusters variant spellings, and proposes what each
one *means*. The merchant reviews that proposal on a screen and signs it off. Only then does
the agent index anything.

**The shopper doesn't need the vocabulary.** Someone who doesn't know what `56–18–145` means
on a pair of glasses can still buy the right pair, because the agent learned from this
merchant's sheet which attributes actually separate the products, and asks about those.

**Consent is enforced by architecture, not by asking the model nicely.** The
`confirm_and_pay` tool is *absent from the model's schema* until the shopper presses a button
that mints a confirmation token. A model that decided to charge someone has no tool to do it
with. Preview → authorize → confirm are three separate endpoints on purpose: that separation
*is* the consent design.

---

## Quick start

Requires Docker, and [`uv`](https://docs.astral.sh/uv/) + Python 3.11+ if you run the
services bare.

```bash
git clone https://github.com/ryuhhhn/toes.git
cd toes
cp .env.example .env          # then add your OPENAI_API_KEY or ANTHROPIC_API_KEY

docker compose up             # postgres + merchant :8001 + agent :8002 + payments :8003
python web/serve.py           # both frontends on :8090
```

Open **http://localhost:8090** for the storefront and
**http://localhost:8090/merchant-console/** for the merchant side.

**Embeddings** default to a local [Ollama](https://ollama.com) (`nomic-embed-text`), so
retrieval costs nothing and needs no network:

```bash
ollama pull nomic-embed-text
```

Set `EMBEDDING_PROVIDER=openai` in `.env` to use the API instead.

<details>
<summary><b>Or run it bare, service by service</b></summary>

```bash
# The database FIRST — host port 5433, not 5432. See "Operational gotchas".
docker compose up -d db

# merchant :8001
cd services/merchant && uv sync && uv run uvicorn app.main:app --port 8001

# agent :8002 — ONE worker, no --reload (SessionStore is in-process)
cd services/agent && uv sync --all-extras && uv run uvicorn app.main:app --port 8002

# payments :8003 — needs Postgres, will not start without it
cd services/payments && uv sync && uv run uvicorn app.main:app --port 8003

# both frontends, one origin
python web/serve.py
```

You can also develop the agent against stubs rather than the real services:

```bash
cd services/agent && uv run uvicorn stubs.mock_services:app --port 9001
```

`stubs/mock_services.py` serves both the merchant and the payment contracts on one port.
Point the agent at it by uncommenting the `:9001` lines in `services/agent/.env`.

</details>

---

## Try it end to end

1. **Upload a catalog.** In the merchant console, upload
   [`services/agent/fixtures/catalogs/power_tools.csv`](services/agent/fixtures/catalogs/) —
   or `tea_and_infusions.xlsx`, or your own sheet. `.csv .tsv .txt .xlsx .xls` all work.
2. **Approve the profile.** The console shows what the agent inferred each column means:
   which are filterable, which are just display, which must be settled before purchase. Fix
   anything wrong and approve. That triggers the index build.
3. **Shop.** Open the storefront and say something vague — *"a gift for someone who does DIY
   at weekends, around £100"*. The agent retrieves wide, shows results immediately, and asks
   the one question that actually narrows *this* catalog. Never a bare interrogation.
4. **Buy.** Press Checkout. A preview appears with the real total. Confirm it. You get a
   receipt, the ledger row lands in Postgres, and stock is written back to the merchant so
   the console reflects the sale.
5. **Try a decline.** `POST /chat/confirm` takes an optional `fail` field that injects a
   failure inside the charge, so a decline is audited exactly like a real one.

---

## Architecture

Four services. Ports are fixed repo-wide.

```
 any spreadsheet         ┌────────────────────────────────┐
 any category      ─────▶│ MERCHANT BACKEND        :8001  │
 any column layout       │ upload · store raw · serve raw │
                         └───────────────┬────────────────┘
                                         │ raw rows, verbatim
                                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ AGENT BACKEND                                                    :8002  │
│                                                                         │
│ INGEST — once per catalog, identical code for every category            │
│   loader ─▶ coerce ─▶ profiler ─▶ canonicalize ─▶ classify ─▶ PROFILE   │
│     │         │          │            │             │           │       │
│  csv/xlsx  currency,  dtype, null  cluster       LLM: tiers,  MERCHANT  │
│  encoding, units,     rate,        variant       layman copy, APPROVES  │
│  headers   list-cells cardinality  spellings     cross-field     │      │
│                                                  rules           ▼      │
│  rows ─▶ enrich (LLM descriptor) ─▶ embed ─▶ INDEX (.npy, per merchant) │
│                                                                         │
│ RUNTIME — per shopper turn                                              │
│   SSE /chat ─▶ agent loop ─▶ tools ─▶ typed events ─▶ storefront        │
│                    └── policy gate: which tools are visible this turn   │
└─────────────────────────────────────────────────────────────────────────┘
              │                                    │
 stock        ▼                                    ▼  preview/authorize/confirm
 write-back   MERCHANT :8001                   PAYMENTS :8003
                                               ledger (Postgres)
```

| Service | Owns | Does not own |
|---|---|---|
| **Merchant** `:8001` | Catalog upload, raw-row storage, serving raw rows, merchant config | Meaning. Retrieval. Anything the shopper sees. |
| **Agent** `:8002` | Ingestion, the profile, retrieval, the agent loop, tools, session, the trust gate | Money movement. The merchant's own console views. |
| **Payments** `:8003` | Preview, consent, charge, the ledger | Anything conversational. It never sees a shopper. |
| **Web** `:8090` | Merchant console, storefront | All business logic. It renders typed events and posts intents. |

The frontends hold no logic that matters. Everything they display arrives as a typed SSE
event — `token` · `tool_start` · `products` · `comparison` · `probe` · `preview` · `receipt` ·
`cart` · `notice` · `error` · `done` — and everything they cause travels as an explicit POST.

Full detail in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**; exact request and response
shapes for every seam in **[docs/CONTRACTS.md](docs/CONTRACTS.md)**.

---

## Repo layout

```
services/
  merchant/    catalog upload, raw storage, merchant config          :8001
  agent/       ingestion, retrieval, agent loop, tools, trust gate   :8002
  payments/    mock Visa preview → authorize → confirm, ledger       :8003
web/
  merchant-console/   upload, catalog table, profile approval screen
  storefront/         shopper chat: SSE client, product cards, consent, receipt
  serve.py            serves both from one origin                    :8090
tests/
  smoke_test.py       cross-service seam tests — the only thing that
                      catches the services drifting apart
docs/
  ARCHITECTURE.md     what each service is for and how a request flows
  CONTRACTS.md        the source of truth for every seam
  agent/              the agent's own design docs and invariants
```

The agent's tools live in [`services/agent/app/tools/`](services/agent/app/tools/):
`search_catalog` · `get_product_details` · `compare_products` · `probe_attributes` ·
`build_cart` · `preview_transaction` · `confirm_and_pay`. Each is a pure function of
`(args, session, profile)`. Tools never decide when they run —
[`agent/loop.py`](services/agent/app/agent/loop.py) does.

---

## Configuration

All configuration is environment variables, read in one place per service. Copy
`.env.example` to `.env` and edit. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` or `anthropic` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | One of these is required by the agent |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` (local, free) or `openai` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is |
| `DB_HOST_PORT` | `5433` | Host port for Postgres — **not 5432**, see below |
| `VISA_MODE` | `mock` | Payments runs against a mock Visa rail |
| `MAX_PROBES_PER_TURN` | `1` | The agent never interrogates; at most one question per turn |
| `MAX_TOOL_ROUNDS` | `5` | Cap on tool calls per shopper turn |
| `PAYMENT_TTL_MINUTES` | `15` | How long a preview stays chargeable |

No secret is committed to this repository and none ever has been. `.env` and `.env.*` are
gitignored, with an explicit `!.env.example` exception.

---

## Tests

Counts below were verified on the current commit.

```bash
cd services/merchant && uv run pytest -q             # 65   no infra
cd services/agent    && uv run pytest -q --tb=line   # 345  no API key, no network, ~60s
cd services/payments && uv run pytest -q             # 25   needs Postgres on 5433

# the one that catches cross-service drift — needs all three services up
uv sync --all-extras && uv run pytest tests/smoke_test.py -q    # 19, skips if the stack is down
```

The agent suite is the safety net for the whole repo: it runs fully offline, and **a drop in
the pass count is a regression even when nothing errors.**

Use `uv sync --all-extras` for the agent — plain `uv sync` skips the `dev` extra that holds
pytest, and a stale `pytest.exe` on `PATH` will then happily run *a different copy of this
service from elsewhere on disk*. That happened once, and the suite passed green against code
that was not in this repository.

Live LLM checks (~$0.14 a run, real API calls):

```bash
cd services/agent && uv run python scripts/live_check.py
```

---

## API surface

Interactive docs at `/docs` on every service.

<details>
<summary><b>Merchant — :8001</b></summary>

| Endpoint | |
|---|---|
| `POST /catalog/upload` | Upload a catalog (multipart: `merchant_id`, `file`). `.csv .tsv .txt .xlsx .xls`, all read as strings. |
| `GET /catalog/raw?merchant_id=&ids=` | **The agent's endpoint.** Raw rows, columns and values untouched. `ids=` filters server-side. |
| `POST /catalog/{merchant_id}/stock` | Write stock back after a sale. |
| `GET /merchants` | Every merchant with a catalog. |
| `GET /catalog` · `GET /categories` · `PATCH /catalog/{product_id}` | Normalized products — **merchant console display only.** |

</details>

<details>
<summary><b>Agent — :8002</b></summary>

| Endpoint | |
|---|---|
| `POST /chat` | The conversation. Server-sent events, typed. |
| `POST /chat/checkout` | Computes a preview directly from the cart. No model in the path. |
| `POST /chat/confirm` | Mints the confirmation token — the only thing that can. Optional `fail` field injects a decline. |
| `POST /ingest/analyze` · `POST /ingest/analyze/upload` | Run the ingestion pipeline over a catalog. |
| `GET /ingest/profile/{merchant_id}` · `PUT /ingest/profile/{merchant_id}` | Read the proposed profile; approve the merchant's edited version. |
| `GET /ingest/report/{merchant_id}` · `GET /ingest/merchants` | Ingestion report; known merchants. |
| `GET /catalog/status` · `POST /catalog/reindex/{id}` · `POST /catalog/sync/{id}` | Index state and rebuilds. |
| `GET /session/{id}` · `GET /trace/{id}` · `GET /sessions` | Inspect a live conversation. |

</details>

<details>
<summary><b>Payments — :8003</b></summary>

| Endpoint | |
|---|---|
| `POST /payment/preview` | Price a cart. Charges nothing. Returns `expires_at`. |
| `POST /payment/authorize` | Consent. `{preview_id, method, proof}`. |
| `POST /payment/confirm` | Capture. |
| `GET /payment/receipt/{transaction_id}` | The receipt. |

Three endpoints, deliberately. **Do not collapse them** — that separation is the consent
design.

</details>

---

## Design rules that are structural, not stylistic

**No niche may be hardcoded.** No file under `services/agent/app/` may name a
domain-specific column, attribute, or value. A test that names a specific product attribute
is a bug in the agent backend. The same rule binds the storefront: no category-specific field
name in rendering code.

**The merchant stores raw rows and serves them untouched; the agent interprets.**
Normalizing at upload destroys the columns the profiler needs, and a normalized row cannot be
un-normalized. Merchant-side normalization exists for the merchant's own product table and
must never sit on the path that feeds the agent.

**The trust gate is enforced by tool absence.** `confirm_and_pay` is filtered out of the
model's tool list until a confirmation token exists, and tokens are minted only by
`POST /chat/confirm`. Prompt instructions are not a security boundary; a missing tool is.

**A button must not be a request for a favour.** Checkout was once a chat message hoping the
model would call `preview_transaction` — so it silently did nothing whenever the model
answered in prose, and did the *same* nothing for four different legitimate refusals.
`POST /chat/checkout` now computes the preview deterministically and refuses with a typed
code (`empty_cart` · `checkout_blocked` · `unknown_session` · `catalog_unavailable`) that the
storefront renders as a sentence.

**Change the contract, then the stub, then the code.** A stub easier to satisfy than
production is not a test double, it is a second implementation nobody ships. That is exactly
what went wrong here once: the stub spoke a more forgiving dialect than the real services, so
hundreds of tests validated a language nobody spoke. `tests/smoke_test.py` exists to catch it
recurring.

---

## Operational gotchas

**Postgres is on host port 5433, not 5432.** On the machine this was built on, a native
PostgreSQL service owns 5432 — and on Windows that does not merely block the container's
bind, it *silently shadows* it. `docker ps` prints `0.0.0.0:5432->5432/tcp` while host
connections reach the native server and fail password auth. Inside the compose network
services still reach `db:5432`; `DB_HOST_PORT` only affects processes outside compose.

**Run the agent with one uvicorn worker and no `--reload`.** `SessionStore` is in-process. A
second worker, or a reload mid-conversation, loses the shopper's live cart.

**Serve the frontends over HTTP, not `file://`.** `file://` origins are opaque, so every
fetch to the agent is a CORS failure and `EventSource` never connects. That is what
`web/serve.py` is for.

---

## Documentation

| | |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Repo map — read it first to find things without grepping |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | What each service is for, how a request flows, what is structural |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | **The source of truth for every cross-service seam** |
| [docs/agent/CLAUDE.md](docs/agent/CLAUDE.md) | The agent's own design and invariants — long and authoritative |
| [docs/agent/INGESTION_AND_RETRIEVAL.md](docs/agent/INGESTION_AND_RETRIEVAL.md) | How a spreadsheet becomes a searchable, filterable catalog |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) · [PROGRESS_LOG.md](PROGRESS_LOG.md) | The build phase by phase, and every bug found along the way |

Built for the Visa hackathon.
