# CLAUDE.md — repo map

Visa hackathon monorepo: a conversational commerce stack. A shopper talks to an AI agent
that helps them **discover → decide → pay** in one conversation.

**This file is a navigation map, not an architecture doc.** Read it first to find things
without grepping.

- Architecture and the flow: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Cross-service request/response shapes: [docs/CONTRACTS.md](docs/CONTRACTS.md)** — the
  source of truth for every seam. Change it before changing code.
- The agent's own design and invariants: [docs/agent/CLAUDE.md](docs/agent/CLAUDE.md) — long,
  authoritative, worth the tokens when working inside that service.
- Work in flight: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and
  [PROGRESS_LOG.md](PROGRESS_LOG.md).

Last verified: 2026-08-30. **The implementation plan is complete — phases 1–8, findings `A`–`Q`.**
Three catalog-freshness bugs found and closed after it: findings `R`–`T` below.

---

## Four services, four owners

Ports are fixed repo-wide.

| Directory | What it is | Entrypoint | Port |
|---|---|---|---|
| [services/merchant/](services/merchant/) | Catalog upload, normalization, storage, merchant config | `app.main:app` | 8001 |
| [services/agent/](services/agent/) | The agent: ingestion analysis, retrieval, agent loop, tools, session, trust gate | `app.main:app` | 8002 |
| [services/payments/](services/payments/) | Mock Visa preview → authorize → confirm, Postgres ledger | `app.main:app` | 8003 |
| [tests/smoke_test.py](tests/smoke_test.py) | **Cross-service seam tests.** The only thing that catches services drifting apart. | `pytest` | — |
| [web/merchant-console/](web/merchant-console/) | Upload, catalog table, **profile approval screen** | `index.html` | 8090 |
| [web/storefront/](web/storefront/) | Shopper chat: SSE client, generic product cards, consent + receipt | `chatbot.html` | 8090 |

[services/agent/stubs/mock_services.py](services/agent/stubs/mock_services.py) is a single
stub on :9001 serving both contracts, and it is what the agent's 342 tests run against. **It
was the source of most cross-service bugs**, because it spoke a more forgiving dialect than
the real services — so the suite validated a language nobody spoke.

It is now converged onto the real contracts (phase 5), and `tests/smoke_test.py` exists to
catch it drifting again. The root `.env` points the agent at the real services; re-enable
stub mode by uncommenting the `:9001` lines in `services/agent/.env`.

**The rule, from [docs/CONTRACTS.md](docs/CONTRACTS.md) §5: change the contract, then the
stub, then the code.** A stub that is easier to satisfy than production is not a test double,
it is a second implementation nobody ships.

---

## Where things are

### `services/agent/` — the agent (the largest, most complete service)

**342 passing tests**, no API key or network needed. Uses `uv`. Everything category-agnostic:
no file under `app/` may name a domain-specific column, attribute, or value.

```
app/
  main.py          create_app(), CORS, lifespan  ·  config.py  ALL env access
  api/             chat.py (POST /chat SSE, POST /chat/checkout, POST /chat/confirm)
                   ingest.py (analyze, upload, get/approve profile, report, merchants)
                   catalog.py (index status, reindex, sync) · session.py · health.py
  agent/           loop.py orchestration · prompt.py · policy.py tool-visibility gates
                   events.py SSE schema (cross-team contract, mirrored in docs/CONTRACTS.md)
  llm/             base.py protocol · openai_client.py · anthropic_client.py · factory.py
  embeddings/      base.py · ollama_provider.py (langchain, nomic-embed-text) · openai · factory
  ingestion/       loader → coerce → profiler → canonicalize → classify → PROFILE
                   schema_map.py roles · bootstrap.py no-LLM fallback · merge.py re-ingest
                   profile_store.py versioned persistence · pipeline.py
  retrieval/       sync.py (pulls merchant catalog) · enrich.py · index.py (numpy .npy)
                   registry.py multi-merchant · filters.py + relaxation · search.py
  tools/           registry.py (one definition → OpenAI + Anthropic schemas)
                   search_catalog · get_product_details · compare_products · probe_attributes
                   build_cart · preview_transaction · confirm_and_pay
  session/         models.py (Cart, Preview, tokens) · store.py in-memory
  clients/         merchant.py · payment.py · http.py   ← the cross-service seams
  audit.py         → data/audit.jsonl
stubs/mock_services.py   merchant + payment stubs, port 9001
fixtures/catalogs/       power_tools.csv, tea_and_infusions.xlsx (2 niches, 2 formats)
data/                    generated profiles + indexes, gitignored
```

Its four design docs live in [docs/agent/](docs/agent/); `services/agent/README.md` points
there.

**Operational constraint:** `SessionStore` is in-process. One uvicorn worker, no `--reload`
during a demo, or carts vanish mid-conversation.

### `services/merchant/` — catalog storage

Restructured in phase 3 from a flat module layout into a proper `app/` package.

```
app/
  main.py          FastAPI app + lifespan; api/catalog.py holds the routes
  storage.py       replace/get/update catalog, raw rows, patch_raw_rows (stock writes)
  tabular.py       reads an upload: .csv .tsv .txt .xlsx .xls, all as strings
  schemas.py       pydantic Product, UploadReport
  normalize.py     CSV → fixed Product shape        ┐ console display only —
  taxonomy.py      keyword rules + confidence       │ never on the path that
  llm_client.py    OpenAI category inference        ┘ feeds the agent
  db/database.py   SQLAlchemy: Merchant/Category/Product; in-memory when DATABASE_URL unset
fixtures/          eyewear_mock_data.csv · eyewear.json (seed data)
tests/             65 passing
examples.py        standalone demo runner
```

Imports are `app.*` and resolve with `services/merchant/` as CWD (its `pyproject.toml` sets
`pythonpath = ["."]`).

### `services/payments/` — money

`app/main.py` (lifespan opens asyncpg pool) · `app/routers/payment_router.py`
(`POST /payment/preview|authorize|confirm`, `GET /payment/receipt/{id}`) ·
`app/models/schemas.py` (Cart, TransactionPreview, UserAuthorizationRequest, Transaction,
ReceiptView) · `app/services/` (ledger_service, user_auth_service, visa_service) ·
`app/db/database.py`. 7 tests, all requiring Postgres.

`preview → authorize → confirm` are deliberately three endpoints — that separation *is* the
consent design. Do not collapse them.

---

## Known breakage — verified, not assumed

Do not spend tokens rediscovering these. **All twenty-one are now closed** — `A`–`D` `N`–`O`
in phases 1–3, `E`–`G` `J` `K` in phase 4, `H` `I` in phase 5, `P` in phases 6–7, and
`R`–`U` on 2026-08-30. They are kept here because knowing what was wrong, and why, is what
stops it coming back. Shapes for all of them are in [docs/CONTRACTS.md](docs/CONTRACTS.md).

Three are worth reading even if you skip the rest: **13** (the stub and the real service
spoke different dialects, so the tests validated a language nobody spoke), **21** (a button
that was really a request for a favour, so it worked most of the time), and **16** (a healthy
container whose port was silently shadowed). Both failed by looking fine.

### Fixed

1. ~~The merchant backend cannot start.~~ **Fixed in phase 1.** `app/main.py` moved beside the
   modules it imports; root `app/` deleted.
2. ~~Root `tests/` cannot collect.~~ **Fixed in phase 1** — now `services/merchant/tests/`,
   65 passing.
3. ~~`app/normalize.py` duplicates `merchant/backend/normalize.py`; `schemas`/`storage` are
   `import *` shims.~~ **Fixed in phase 1** — the duplicate and both shims are deleted.
4. ~~Three top-level packages named `app`.~~ **Reduced in phase 1**: the root one is gone, so
   only the agent's and payments' remain and they never share a path. Renaming them is
   deferred until after the demo — a recorded decision, not an oversight.
5. ~~No root `.gitignore`; `payments/.gitignore` does not cover `.env`.~~ **Fixed in phase 1.**
6. ~~`README.md` contains unresolved merge conflict markers~~ and describes a React + Vite
   template that is not in this repo. **Fixed in phase 1.**

7. ~~Merchant `GET /catalog` returns a bare list.~~ **Fixed in phase 4.** `GET /catalog/raw`
   serves `{merchant_id, id_column, row_count, rows}` with columns and values untouched.
8. ~~`/catalog` has no `ids=` param.~~ **Fixed in phase 4.** `/catalog/raw?ids=` filters
   server-side, so pre-charge reverification (invariant 5) no longer degrades silently.
9. ~~`GET /merchants` does not exist.~~ **Fixed in phase 4.**
10. ~~`DATABASE_URL` captured at import time.~~ **Fixed in phase 4** — the engine is built in
    the lifespan. A configured-but-unreachable database is now a hard startup failure rather
    than a silent fall back to memory.
11. ~~The merchant seed pointed at post-normalization output.~~ **Fixed in phase 4** — it
    reads `fixtures/eyewear_mock_data.csv`.
12. ~~`PaymentClient` speaks the stub's dialect.~~ **Fixed in phase 5.** Item key is
    `product_id`, authorize sends `{preview_id, method, proof}`, and `confirm_and_pay` reads
    `amount`/`created_at`, building line items from `session.active_preview.items`. **The
    stub was converged to the real dialect too** — its fat confirm response was what let the
    agent read four fields production does not have.
13. ~~Error envelopes differ in kind.~~ **Fixed in phase 5.** Real payments sends
    `detail={code, message}` everywhere, and `?fail=` works against it — injected inside the
    charge so a decline is audited like a real one. Reachable from a conversation via the
    optional `fail` field on `POST /chat/confirm`.
14. ~~Two expiry clocks.~~ **Fixed in phase 5.** The preview carries `expires_at`, derived
    from `created_at` + `PAYMENT_TTL_MINUTES` as a computed field so it cannot drift.
    `PREVIEW_TTL_SECONDS` is now only a fallback.
16. ~~A native PostgreSQL 18 service holds port 5432.~~ **Worked around.** It does not merely
    block the bind — on Windows it **silently shadows** the container's publish, so
    `docker ps` shows `0.0.0.0:5432->5432/tcp` while host connections reach the native server
    and fail password auth. The container is published on **5433** (`DB_HOST_PORT`).
15. ~~Neither frontend calls a backend.~~ **Fixed in phases 6–7.** The console uploads to the
    merchant, triggers analysis, and has a full profile approval screen; the storefront
    streams `POST /chat` and renders from typed events only. Both share `web/config.js` and
    are served by `web/serve.py` on **:8090**.

---

### Catalog freshness — findings `R`–`T`, closed 2026-08-30

Three separate bugs, one shared shape: **a screen describing a catalog that is no longer
there.** None of them raised anything. All three looked exactly like working software.

17. ~~The merchant console's product list went stale.~~ **Fixed.** It read `GET /catalog`
    (normalized), which is all-or-nothing: a sheet whose id/title/price columns could not
    be mapped normalized to *nothing*, and the upload path then left the **previous
    upload's products in place**. The console showed last week's catalog while the agent
    sold from this week's. `POST /catalog/upload` now always resets that view — to the
    products, or to empty — and the console's table renders `GET /catalog/raw` instead,
    with headers taken from the sheet's own columns. It also refreshes itself every 10s
    while it is the visible view, so a sale or another tab's upload shows up.
18. ~~Excel uploads were impossible.~~ **Fixed.** `POST /catalog/upload` called
    `pd.read_csv` and the console's picker accepted only `.csv`, so a merchant who keeps
    their catalog in a spreadsheet — which is most of them — could not use the product
    without exporting first. `app/tabular.py` reads `.csv .tsv .txt .xlsx .xls`, all with
    `dtype=str`, because pandas' type inference is exactly the coercion §0 forbids.
19. ~~A sale never reached the shop.~~ **Fixed.** Nothing decremented stock, so the
    console showed upload-day numbers forever and a one-of-a-kind item stayed buyable.
    `confirm_and_pay` now writes stock back through `POST /catalog/{id}/stock` (§1.5)
    **after** the charge captures — best-effort, never blocking a receipt, and the agent
    names the column because the merchant does not know which one means stock.
20. ~~A rebuilt index could use a profile describing a different spreadsheet.~~ **Fixed.**
    A profile maps roles onto columns *by name*. Replace the sheet with a differently
    shaped one and `registry._build` reused the stored profile anyway, producing an index
    whose every filter named a column that was not there. **Search returned zero matches
    for a shop full of stock**, and the only symptom was an agent saying it had none.
    `IndexRegistry._describes` now re-derives when the id/title/price columns are absent.

### The checkout button — finding `U`, closed 2026-08-30

21. ~~Checkout sometimes did nothing at all.~~ **Fixed.** The storefront's Checkout button
    sent the chat message *"I'm ready to check out. Show me the total."* and waited for
    the model to call `preview_transaction`. That made a button press a request for a
    favour. The panel stayed shut whenever the model answered in prose — and stayed shut
    *identically* for every legitimate refusal, because `policy.available_tools`
    **withdraws** the preview tool when the basket is empty or a
    `required_before_purchase` field is unsettled. Same nothing, four different reasons.
    Clicking mid-stream was a fifth: `send()` drops anything sent while a turn is open.

    A preview is a deterministic function of the cart, so **`POST /chat/checkout`** now
    computes one directly, with no model in the path, and refuses with a typed code
    (`empty_cart` · `checkout_blocked` · `unknown_session` · `catalog_unavailable`) the
    storefront renders as a sentence. Every control also greys out for the duration of a
    turn, so a click can no longer be silently swallowed.

    **This does not weaken the trust gate.** A preview charges nothing. The gate is that
    `confirm_and_pay` is absent from the model's schema until `POST /chat/confirm` mints
    a token, and this endpoint mints none. Preview and charge are now both button-driven
    endpoints rather than one button and one hope.

---

## The overlap that is a design conflict, not a bug — **resolved**

Merchant `normalize.py` + `taxonomy.py` + `llm_client.py` infer categories and coerce rows
into a **fixed nine-field `Product`**. The agent's `app/ingestion/` does the same job
category-agnostically.

Normalizing at upload **destroys the raw columns the agent's profiler needs** — the agent's
whole premise is that column layout is arbitrary and derived, and a normalized row cannot be
un-normalized.

**Resolved position, now written down in [docs/CONTRACTS.md](docs/CONTRACTS.md) §0:**
the merchant stores raw rows and serves them untouched; the agent interprets. Merchant
normalization is kept for the merchant console's own product table and must never sit on the
path that feeds the agent. `GET /catalog/search` is retired.

---

## Running things

The whole stack in one command:

```bash
docker compose up          # postgres + merchant :8001 + agent :8002 + payments :8003
python web/serve.py        # both frontends on :8090
```

Or bare, service by service:

```bash
# the database FIRST — host port 5433, not 5432 (see finding 16)
docker compose up -d db

# merchant
cd services/merchant && uv run uvicorn app.main:app --port 8001

# agent (uv)
cd services/agent && uv sync && uv run uvicorn app.main:app --port 8002   # ONE worker

# the stubs the agent was built against
cd services/agent && uv run uvicorn stubs.mock_services:app --port 9001

# payments (needs Postgres)
cd services/payments && uv run uvicorn app.main:app --port 8003

# both frontends, one origin
python web/serve.py        # http://localhost:8090
```

Tests:

```bash
# the one that catches cross-service drift — needs all three services up
uv sync --all-extras && uv run pytest tests/smoke_test.py -q   # 19; skips if the stack is down

cd services/merchant && uv run pytest -q             # 65, no infra
cd services/agent    && uv run pytest -q --tb=line   # 342, no API key, no network, ~30s
cd services/payments && uv run pytest -q             # 25, needs Postgres on 5433
```

Live LLM checks (~$0.14/run): `cd services/agent && uv run python scripts/live_check.py`

---

## Conventions worth knowing before you edit

- Agent backend: async throughout, pydantic at every boundary, one shared
  `httpx.AsyncClient`, all env through `config.py`, all LLM calls through `llm/factory.py`,
  tool schemas defined once in `tools/registry.py`.
- Tools are pure functions of `(args, session, profile)`. They never decide when they run —
  `agent/loop.py` does.
- The trust gate is enforced by **tool absence**, not prompt instructions:
  `confirm_and_pay` is filtered out of the model's tool list until a confirmation token
  exists, and tokens are minted only by `POST /chat/confirm`.
- A test that names a specific product attribute is a bug in the agent backend. The same rule
  binds the storefront: no category-specific field name in rendering code.
- The agent test count is the safety net for the whole repo. **342** is the number; a drop is
  a regression even when nothing errors. Use **`uv sync --all-extras`** for the agent — plain
  `uv sync` skips the `dev` extra holding pytest, and a stale `pytest.exe` wrapper then runs
  a *different copy of this service* from elsewhere on disk. That happened, and the suite
  passed green against code that was not in this repository. `pythonpath = ["."]` in
  `pyproject.toml` now pins the source regardless.
