# Implementation plan

Execution plan for the findings in the integration review
(<https://claude.ai/code/artifact/dab2c7dc-70c3-43a4-8eaf-3705f83721fb>).
Finding refs `A`–`Q` below point back at that document.

Written to be executed by me, in order. Each phase ends with a verification gate that must
pass before the next one starts.

> ## ✅ COMPLETE — all eight phases closed, 2026-08-29
>
> Findings `A`–`Q` are all fixed. What actually happened, phase by phase, is in
> [PROGRESS_LOG.md](PROGRESS_LOG.md) — **read that, not this file**, for the current state.
> This document is kept as the record of what was planned, including the several places
> where execution had to depart from it.
>
> Final counts: agent **316** · merchant **47** · payments **25** · cross-service smoke
> **15**. Note 316, not the 299 this plan quotes and not the 312 recorded at the pause
> gate — see the phase 6 entry, where the agent's test suite turned out to be importing a
> different copy of the service from elsewhere on disk.

---

## Decisions locked

| Question | Decision |
|---|---|
| File reorganization | **Full restructure** — `services/{merchant,agent,payments}` + `web/` + `docs/` |
| "Merchant deconflict page" | **Full profile approval screen** — roles, fields, tiers, layman copy, required flags, cross-field rules |
| Frontend stack | **Vanilla HTML/CSS/JS** — no build step, matches both existing frontends |
| Merchant `normalize`/`taxonomy`/`llm_client` | **Keep, console-display only** — never on the path that feeds the agent |

Ports, fixed everywhere from here on:

| Service | Port |
|---|---|
| merchant | `8001` |
| agent | `8002` |
| payments | `8003` |
| stubs (`mock_services`) | `9001` |

---

## Setup — what this machine needs

Verified on 2026-08-29, not assumed. **Almost everything is already here.**

### Already good — do nothing

| Thing | State |
|---|---|
| **Ollama** | v0.20.3, **running** on `:11434` |
| **`nomic-embed-text`** | Pulled (274 MB). Matches `OLLAMA_EMBED_MODEL` in the existing `.env`, and matches the two existing indexes (`ollama:nomic-embed-text`, 768-dim). Consistent — no reindex needed. |
| **`OPENAI_API_KEY`** | Set in `customer/backend/.env` (164-char project key), `LLM_PROVIDER=openai`, `OPENAI_MODEL=gpt-4.1` |
| **uv** | Installed, and `customer/backend/.venv` already exists |
| **Python** | 3.13.6, with fastapi + pandas + sqlalchemy importable system-wide (enough to run the merchant bare) |
| **Node** | Installed — and not needed, since the console is vanilla JS with no build step |

### The one thing you must do

**Start Docker Desktop.** Docker 28.3.2 is installed but the engine isn't running
(`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`).
Postgres comes from `docker compose up -d db` — no separate Postgres install, no `psql`
needed.

That's the whole list. You do **not** need to pull an embedding model, buy anything, or
install a database.

### Do you need Postgres and Ollama at all?

| Need | Required for | Not required for |
|---|---|---|
| **Postgres** (Docker) | Payments — `init_pool()` runs in its lifespan, so the service will not start without it. Phases 5, 8. | Phases 1–4. The merchant has a working in-memory fallback. |
| **Ollama** | Real retrieval quality — phases 6, 7, and the demo. Already running. | The 299 agent tests, which need no API key and no network. |
| **`OPENAI_API_KEY`** | The LLM classify step and agent conversation — phases 6, 7. Already set. | The 299 agent tests. |
| **`ANTHROPIC_API_KEY`** | Nothing. Currently empty, and `LLM_PROVIDER=openai`. Leave it. | Everything. |

**So: later, not now.** Phases 1–4 need nothing beyond what's on the machine. That is exactly
where the plan pauses.

### The unified `.env` — yes, and here's how it actually resolves

One root `.env` is the source of truth. Two consumers, so it has to work both ways:

- **Composed runs** — `docker-compose.yml` reads it with `env_file: .env`.
- **Bare `uvicorn` runs** — each service's `config.py` uses
  `SettingsConfigDict(env_file=(".env", "../../.env"))`, so a service-local `.env` still wins
  and the root one is the fallback. Without this the root file is decorative: pydantic-settings
  resolves `env_file` relative to the process CWD, so a root `.env` would simply never be read
  by a service started from its own directory.

`.env` goes into the root `.gitignore` in phase 1, **before** any file containing a key is
written.

---

## ⏸ The pause gate

The plan splits in two. **I stop at the gate and hand it back to you.**

**Block A — phases 1–4.** Needs nothing new. File surgery, contracts, the move, and the
merchant's raw-row work, all verifiable with the in-memory store and the existing agent venv.
At the end of Block A I will have written `.env.example` and generated a root `.env`
pre-populated with every value I can carry over from `customer/backend/.env` — including your
existing OpenAI key, which stays local and gitignored.

**At the gate I hand you:** the generated `.env` with a short list of anything still blank, and
a three-line setup checklist (realistically: start Docker Desktop).

**You then:** start Docker Desktop, skim the `.env`, paste or correct anything you want
different.

**Block B — phases 5–8.** I resume and test against real services: real Postgres, real
payments, real charges, real conversations.

Nothing in Block B gets written before the gate. Unverified integration code sitting in the
tree is how the current mismatches happened in the first place.

---

## Working protocol — logging and test economy

### The log doc

`PROGRESS_LOG.md` at the repo root, **appended after every phase**, in the same spirit as the
agent's `PROGRESS.md`. If you run out of tokens mid-plan, a fresh session reads
`CLAUDE.md` → `IMPLEMENTATION_PLAN.md` → `PROGRESS_LOG.md` and resumes without re-deriving
anything.

Each entry records:

- **Phase + status** — complete / partial / blocked
- **Files touched** — paths only, no diffs
- **Verified** — the exact command run and its result (`299 passed`), not a claim that it
  passed
- **Decisions made mid-flight** that this plan doesn't already cover
- **Next action** — the single specific thing to do on resume
- **Anything left broken on purpose**, so it isn't mistaken for an oversight

It gets written *as each phase completes*, never batched at the end — batching is exactly what
loses the state a context reset was going to destroy.

### Test economy

The 299-test suite runs in ~28s. The cost is not time, it's the output landing in context.
Rules:

1. **Default command:** `uv run pytest -q --tb=line 2>&1 | tail -15`. Gives the pass count and
   one line per failure. Never plain `pytest -v`.
2. **Assert on the count.** `299 passed` is the signal. A drop is a regression even if nothing
   errors.
3. **Targeted subsets during a phase**, full suite only at phase gates. Touching
   `retrieval/` means `pytest tests/test_retrieval.py -q`, not the world.
4. **`scripts/live_check.py` runs exactly twice** — once just after the pause gate to confirm
   the real environment, once at the end. It costs ~$0.14 of real API spend and produces heavy
   output; it is not a per-phase check.
5. **Batch independent verifications into one tool call.** Three greps that don't depend on
   each other are one call, not three.
6. **Never re-read a file already in context**, and prefer `sed -n '20,60p'` or `grep -n` over
   `cat` on anything large.
7. **Failures get read in full.** Economy applies to green output, never to red.

---

## Storage verdict — how the merchant backend actually persists

Asked directly: JSON, Postgres, or both? **It declares three paths and runs on exactly one.**

| Path | Status | Detail |
|---|---|---|
| **Postgres via SQLAlchemy** | Plumbed, never configured | Complete SQL branches in `storage.py` for replace/get/update/search/categories. `psycopg[binary]` is in `requirements.txt`. But there is no `.env`, no `.env.example`, and nothing in any compose file that sets `DATABASE_URL` for the merchant. **Dead in every current run.** |
| **In-memory dict** | The actual store | `_DB: dict[merchant_id -> dict[product_id -> product]]`. Process-local. Dies on every restart. Not shared across workers. |
| **`eyewear.json` seed** | Dead code | `_seed_catalog_from_json()` resolves to `toes/eyewear.json`; the file is at `merchant/backend/eyewear.json`. Broken by the same reorg commit as finding `A`. It hits `if not json_path.exists(): return` and seeds nothing, silently. |

So: **both, nominally; neither, actually.** Every run today is in-memory and empty.

Two things make this worse than it looks:

1. **`DATABASE_URL` is captured at import time.** `database.py` reads it into a module-level
   constant and builds `engine` at import. `_use_sql()` re-reads the env var at call time but
   still requires `engine is not None`, which was decided at import. So a `.env` loaded after
   module import silently yields in-memory mode with no warning anywhere. Anyone who sets
   `DATABASE_URL` and expects Postgres will debug a phantom.
2. **`eyewear.json` is post-normalization output** — 50 products already coerced into the
   fixed nine-field shape (`id, merchant_id, title, description, price, category, attributes,
   image_url, stock`). It is therefore useless as agent input, which needs the raw sheet.
   The seed should come from `eyewear_mock_data.csv` instead.

### What I will do about it

- Add `services/merchant/app/config.py` (pydantic-settings), read `DATABASE_URL` **lazily at
  app startup**, not at import. Build the engine there.
- **Postgres becomes the default** for the composed stack. Payments already requires Postgres
  and already ships a compose file, so the instance exists — merchant gets its own database on
  it. This matters for the demo: today, uploading a catalog and then restarting the backend
  loses the catalog while the agent's index still points at it.
- **Keep in-memory as the explicit no-infra fallback** for tests and bare `uvicorn` runs.
- **Make the mode visible** — log it at startup and report it on `GET /health` as
  `{"storage": "postgres" | "memory"}`, so nobody ever again debugs a store they aren't using.
- Fix the seed path and repoint it at `eyewear_mock_data.csv`, seeding **raw rows**.

---

## Target layout

```
toes/
├─ README.md                      rewritten — conflict markers out, React template out
├─ CLAUDE.md                      already written — repo map
├─ IMPLEMENTATION_PLAN.md         this file
├─ .gitignore                     new, at root
├─ .env.example                   new — every service's vars in one file
├─ docker-compose.yml             moved up from payments/, now all services + db
│
├─ docs/
│  ├─ ARCHITECTURE.md             new
│  ├─ CONTRACTS.md                new — ★ the cross-team source of truth
│  └─ agent/                      CLAUDE · BUILD_PLAN · PROGRESS · INGESTION_AND_RETRIEVAL
│
├─ services/
│  ├─ merchant/                   :8001
│  │  ├─ pyproject.toml           replaces requirements.txt
│  │  ├─ app/
│  │  │  ├─ main.py               ◀ root app/main.py, imports repointed
│  │  │  ├─ config.py             new — pydantic-settings
│  │  │  ├─ api/catalog.py        new — routes lifted out of main.py
│  │  │  ├─ db/database.py        + raw_rows table
│  │  │  ├─ storage.py            + raw-row read/write
│  │  │  ├─ schemas.py
│  │  │  └─ normalize.py taxonomy.py llm_client.py   console-display only
│  │  ├─ fixtures/                eyewear_mock_data.csv · eyewear.json
│  │  └─ tests/                   ◀ root tests/
│  │
│  ├─ agent/                      :8002 — was customer/backend
│  │  ├─ pyproject.toml  uv.lock
│  │  ├─ app/                     unchanged; 299 tests must stay green
│  │  ├─ stubs/ fixtures/ scripts/ tests/
│  │  └─ data/                    generated, gitignored
│  │
│  └─ payments/                   :8003
│     ├─ pyproject.toml           + a real [project] table
│     ├─ app/
│     └─ tests/
│
├─ web/
│  ├─ serve.py                    new — one static server for BOTH frontends, :8080
│  ├─ merchant-console/           was merchant/frontend
│  │  ├─ index.html               + a fourth "Review" nav view
│  │  ├─ app.js                   rewritten — real API calls
│  │  ├─ review.js                new — the approval screen
│  │  └─ styles.css
│  └─ storefront/                 REPLACED wholesale — see "Storefront drop-in" below
│     ├─ chatbot.html             141 lines: chat + order + details + payment + receipt
│     ├─ chatbot.js               357 lines — transport to be grafted on in phase 7
│     ├─ chatbot.css              585 lines — keep as-is, it is the visual language
│     ├─ atlas-widget.js          iframe embed for a merchant's own site
│     └─ products.csv             DELETE in phase 7 — the last hardcoded catalog
│
├─ app/      DELETE
└─ tests/    DELETE
```

Inner packages stay named `app`. Once root `app/` is gone the three never share a path, and
renaming them costs the agent an edit across ~60 files for a hazard that deleting one
directory already removes. Revisit after the demo.

---

## Storefront drop-in — recorded 2026-08-29, mid-Block-B

A new customer frontend was dropped into the repo at `customer-frontend/customer/frontend/`
and **replaces `web/storefront/` wholesale.** It is a straight upgrade: 1,155 formatted
lines against the 19 minified ones that were there, and it adds the entire post-cart UI the
old mockup did not have.

Moved into `web/storefront/`; `customer-frontend/` deleted. Two things were corrected on
the way in, both because they collide with decisions already locked above:

1. **Its `serve.py` defaulted to port 8002 — the agent's port.** Promoted to `web/serve.py`
   on **:8080**, serving the whole `web/` tree so the storefront and the console share one
   origin and can link to each other. A single origin is also one CORS entry, not two.
2. **`chatbot.html` linked to a sibling `index.html`** that does not exist in
   `web/storefront/`. Repointed at `../merchant-console/index.html`.

### What the drop-in gives us for free

Real UI, already styled, for every step after search: an order panel with quantity steppers,
a delivery-details form, a card-details form, a Visa Secure payment panel with a tokenized
card mock, an explicit consent checkbox gating the confirm button, and a receipt card.
**The consent checkbox is worth calling out — it is exactly the trust gate the backend
already enforces by tool absence, and it was not in the old mockup at all.**

### What is fake in it, and must be replaced in phase 7

Everything below the UI. There is no `fetch` and no `EventSource` in the file.

| Fake | Replace with |
|---|---|
| `products.csv` + `fallbackProducts` hardcoded in the JS | `products` SSE event |
| `setTimeout(…, 700)` "Atlas is thinking…" | real `token` events streaming in |
| `showComparisonResults()` — a hardcoded paragraph naming two invented products | `comparison` event |
| `simulate()` — `setTimeout` to a fake approval | `POST /chat/confirm` → `receipt` event |
| `€` and `EUR` hardcoded in six places | server-supplied currency, never recomputed client-side |
| Receipt `#NV-2042` and `€129.00` baked into the HTML | `receipt` event fields |
| Card/expiry/CVV inputs | **See the consent-vs-card decision below.** |

### Three problems in it that are not just "unwired"

1. **`chatbot.js` defines `simulate()` twice** (lines ~300 and ~340). The second silently
   wins. Dead code that reads as live code — delete both in phase 7 rather than fixing one.
2. **It hardcodes product categories.** `p.image` indexes CSS classes named
   `headphones` / `buds` / `watch`, and a suggestion chip reads "Compare headphones vs
   earbuds". This **violates the category-agnostic rule** that binds the storefront exactly
   as it binds the agent backend (`CLAUDE.md`: "no category-specific field name in rendering
   code"). A storefront that can only draw headphones breaks the core claim as surely as a
   backend that hardcodes "screen size". Phase 7 replaces the class-per-category with a
   single neutral placeholder and derives every label from the profile's roles.
3. **It collects card number, expiry and CVV.** Payments has no such fields — `authorize`
   takes `{preview_id, method, proof}`, and `AuthMethod.EXPLICIT_CONFIRM` *is* the consent.
   Collecting a PAN we then discard is theatre that teaches a judge the wrong thing about
   the architecture, and typing a real card into it would be actively bad. **Decision: keep
   the delivery-details step, keep the Visa panel and the consent checkbox, drop the
   card-details step** — the tokenized-card mock already communicates "Visa" without
   pretending to take a PAN. The consent checkbox becomes the sole gate on the confirm
   button, which is the true architecture.

Phase 7 below is rewritten against this file rather than against the old empty mockup.

---

# ═══ BLOCK A — no external setup needed ═══

Phases 1–4. Everything here is verifiable with what is already on the machine: the existing
agent venv, the merchant's in-memory store, and no network. Docker stays off, and nothing
costs money.

---

## Phase 1 — Stop the bleeding

**Refs** `A` `B` `C` `N` `O` · **Gate:** merchant imports and its tests collect.

Nothing downstream can be verified while the merchant can't import, so this lands first and
alone.

1. `git mv app/main.py merchant/backend/main.py`. Repoint its imports from
   `app.normalize` / `app.schemas` / `app.storage` to the flat `normalize` / `schemas` /
   `storage` that already sit beside it.
2. Delete `app/` entirely — `normalize.py` (the near-duplicate), the two 23-byte
   `from x import *` shims, `__init__.py`.
3. `git mv tests merchant/backend/tests`. Their `sys.path.insert(parents[1])` now resolves
   correctly; check whether `parents[1]` still points where they need after the move and fix
   the index if not.
4. Fix `_seed_catalog_from_json()`'s path (it will be re-pointed at the CSV in phase 4; for
   now just stop it resolving to a nonexistent file).
5. Restore a root `.gitignore` covering `.env`, `__pycache__/`, `*.py[cod]`, `.venv/`,
   `.pytest_cache/`, `node_modules/`, `data/`, `*.npy`, `.DS_Store`.
   Add `.env` to `payments/.gitignore`.
6. Rewrite `README.md` — strip the `<<<<<<< HEAD` / `>>>>>>> 7ac3c08` markers, drop the React
   + Vite section describing a template that isn't here, replace with the port map and real
   run commands.
7. Create `PROGRESS_LOG.md` and write its first entry. The `.gitignore` in step 5 must land
   **before** any later phase writes a file containing a key.

**Verify:** `python -c "import main"` from `merchant/backend/` succeeds ·
`pytest merchant/backend/tests -q` collects and reports (failures are fine here, collection
errors are not) · `grep -rn "<<<<<<<" README.md` returns nothing.

---

## Phase 2 — Write the contracts down

**Refs** `E` `F` `G` `H` `I` `J` `K` `L` · **Gate:** `docs/CONTRACTS.md` exists and every
later phase cites it.

Every mismatch in the review exists because three teams each held a private idea of the same
interface. The contract gets written *before* the code that implements it.

`docs/CONTRACTS.md` fixes:

- `GET /catalog/raw` and `GET /merchants` request + response shapes (appendix A below)
- The three payment endpoints, with the field names each side actually sends
- The SSE event schema, lifted from the agent's `agent/events.py` — it is already the
  cross-team contract, it just doesn't live anywhere shared
- **The normalization boundary:** merchant stores raw rows and serves them untouched; the
  agent derives all meaning. Merchant `normalize`/`taxonomy` render the merchant's own product
  table and nothing else.
- **Audit authority** (`L`): the payments ledger is authoritative for money; the agent's
  `audit.jsonl` is authoritative for what the agent did and why; they join on `preview_id`.

Also write `docs/ARCHITECTURE.md` (the four roles + the flow diagram, lifted from the agent's
`CLAUDE.md` which currently owns it by accident).

---

## Phase 3 — The move

**Refs** target layout · **Gate:** all three suites pass from their new homes.

Pure `git mv`, zero logic changes, one commit, so everyone rebases exactly once. Doing it now
rather than last means phases 4–7 are written in final locations.

1. `services/merchant/` ← `merchant/backend/`, with `main.py` and `tests/` from phase 1, and
   `eyewear_mock_data.csv` / `eyewear.json` into `services/merchant/fixtures/`.
2. `services/agent/` ← `customer/backend/`. Its inner `app/` is untouched, so imports don't
   move. `pyproject.toml`'s `testpaths` and `[tool.hatch.build.targets.wheel]` still resolve
   relatively — confirm, don't assume.
3. `services/payments/` ← `payments/`. Add a real `[project]` table to its `pyproject.toml`
   (`M`) — it currently has only pytest config.
4. `web/merchant-console/` ← `merchant/frontend/`, `web/storefront/` ← `customer/frontend/`.
5. `docs/agent/` ← the agent's four markdown files. Leave a one-line pointer in
   `services/agent/` so anyone working in there still finds them.
6. `docker-compose.yml` up to the root.
7. Update `CLAUDE.md` (every path in it changes) and `README.md`.

**Verify:** `cd services/agent && uv run pytest` → **299 passed**, the number from
`PROGRESS.md`; any drop means the move broke something ·
`cd services/payments && pytest` passes · `cd services/merchant && pytest` collects ·
`git status` shows renames, not delete+add.

---

## Phase 4 — Merchant: raw rows and the catalog contract

**Refs** `E` `F` `G` `J` `K` + the storage verdict · **Gate:** the agent syncs a real catalog
off the real merchant.

This is the substantive backend work. Runs in parallel with phase 5.

### 4a. Config and storage mode

- `services/merchant/app/config.py` — pydantic-settings, `database_url: str | None`,
  `seed_on_startup: bool`, read at startup not import.
- `database.py`: engine built lazily from settings. `_use_sql()` stops re-reading `os.environ`.
- `GET /health` returns `{"status": "ok", "storage": "postgres" | "memory", "merchants": n}`.
- Log the storage mode on startup at INFO.

### 4b. Raw-row storage

- New `raw_rows` table: `merchant_id` (PK), `id_column`, `rows` (JSON), `row_count`,
  `uploaded_at`, `source_filename`. One row per merchant — a catalog upload replaces it,
  matching the existing `replace_catalog` semantics.
- `storage.py`: `replace_raw_rows(merchant_id, rows, id_column, filename)`,
  `get_raw_rows(merchant_id, ids=None)`, `list_merchants()`. In-memory branch alongside the
  SQL branch, same as the existing functions.
- `id_column` detection: port `_first_unique_column` from the agent's
  `stubs/mock_services.py` — first column that is fully populated and fully unique. It is
  storage-level id detection only; deriving roles properly stays the agent's job.
- `POST /catalog/upload` stores raw rows **before** normalizing, so the raw sheet survives
  regardless of what normalization does to it.

### 4c. New endpoints

- `GET /catalog/raw?merchant_id=&ids=` → `{merchant_id, id_column, row_count, rows}`.
  `ids` is comma-separated; absent means the whole catalog. **This is the finding `F` fix** —
  it must return only the ids requested.
- `GET /merchants` → `{merchants: [{merchant_id, row_count, id_column}]}`.
- Retire `GET /catalog/search` (the agent owns retrieval; nothing else calls it).
- Add `CORSMiddleware` — the merchant app currently has none, and the console will be served
  from a different origin. The agent already allows `*`.

### 4d. Seed

Repoint `_seed_catalog_from_json` at `fixtures/eyewear_mock_data.csv`, seeding raw rows and
then normalized products. Keep `eyewear.json` as a fixture for the normalization tests.

### 4e. Agent side

- `clients/merchant.py`: `_get("/catalog", …)` → `_get("/catalog/raw", …)` in both
  `fetch_catalog` and `fetch_by_ids`. The response shape it already expects is unchanged, so
  this is a path edit.
- Set `MERCHANT_BASE_URL=http://localhost:8001` in the root `.env.example`.

### 4f. The unified `.env`

- Write root `.env.example` — every var for all four services, with the port map and inline
  comments on which ones actually matter.
- Generate root `.env` from it, pre-populated with every value carried over from
  `customer/backend/.env` (the OpenAI key included — it stays local, and `.gitignore` from
  phase 1 already covers it). Add the new `DATABASE_URL` entries defaulted to the compose
  credentials.
- Point both `config.py` files at `env_file=(".env", "../../.env")` so the root file is
  actually read by bare `uvicorn` runs, not just by compose.

**Verify:** upload `eyewear_mock_data.csv` to the real merchant, then
`GET /catalog/raw?merchant_id=eyewear_co&ids=<two real ids>` returns **exactly two rows** ·
raw column names survive the round trip byte-for-byte · the agent's
`POST /catalog/sync/eyewear_co` builds an index off the real merchant · a fresh
`reverify_cart` against a stock-zeroed row reports it out of stock.

Run this sync with `use_llm=false` so it takes the deterministic `bootstrap.py` path.
It proves the plumbing without spending anything; the LLM classify pass is a Block B concern.

---

# ⏸ PAUSE — hand back to you

Block A is done and verified. Nothing below has been written yet.

**What I hand you:**

- Root `.env`, pre-filled, with a short list of anything still blank
- `PROGRESS_LOG.md` with entries for phases 1–4
- A green test count for all three services from their new locations

**What you do:**

1. **Start Docker Desktop.** The only genuine install step. Then `docker compose up -d db`.
2. Skim the `.env` — correct anything you want different. Your OpenAI key will already be in
   it; `ANTHROPIC_API_KEY` stays blank unless you want to flip `LLM_PROVIDER`.
3. Tell me to continue.

**Then I resume at phase 5** and run `scripts/live_check.py` once (~$0.14) to confirm the real
environment before touching payment code.

---

# ═══ BLOCK B — needs the setup above ═══

Phases 5–8. Real Postgres, real payments, real charges, real conversations.

---

## Phase 5 — Payments: close the money contract

**Refs** `H` `I` · **Gate:** preview → authorize → confirm end to end against real payments.

Parallel with phase 4. Small, mechanical, high value.

### Payments side

- `Cart`: add `currency: str = "USD"`; use it in `TransactionPreview` instead of the
  hardcoded literal.
- `TransactionPreview`: add `expires_at: datetime`, computed from `PAYMENT_TTL_MINUTES` —
  the same source `ledger_service.is_expired` already uses, so there is one clock (`I`).
- Optional but worth it for the demo: `?fail=<code>` injection on the three endpoints,
  matching the stub's codes (`insufficient_funds`, `card_declined`, `network_error`,
  `expired_preview`), so a declined card stays a scripted beat rather than a story.
- Leave `preview → authorize → confirm` as three endpoints. That separation is the consent
  design; the router says so and it's right.

### Agent side — `clients/payment.py`

- `preview()`: item key `id` → **`product_id`**. Keep sending `currency` (payments now accepts it).
- `authorize()`: body becomes `{preview_id, method: "explicit_confirm", proof: true}`.
  Drop `session_id`/`user_id` from the body — `AuthMethod.EXPLICIT_CONFIRM` already exists in
  the payments enum, which is exactly this flow.
- `confirm()` response mapping in `tools/confirm_and_pay.py`: read `amount` (not `total`) and
  `created_at` (not `timestamp`); build the receipt's line items from
  `session.active_preview.items`, which the agent already holds, rather than from a response
  that carries none.
- `preview_transaction.py`: use the server's `expires_at` instead of minting one from
  `PREVIEW_TTL_SECONDS`. Demote that setting to a fallback for when the stub is in use.
- `receipt()`: unwrap `ReceiptView{transaction, authorization, events}`. Currently unused, so
  this is pre-emptive — but leaving a latent mismatch in a payment client is how it gets found
  at the worst moment.

**Verify:** `PAYMENT_BASE_URL=http://localhost:8003`, then a full agent conversation through
to a charge · the receipt event shows the real total, real line items and a real timestamp
(all three are currently 0.0 / empty / blank) · `?fail=card_declined` leaves the cart intact
and the agent offers an alternative · the expiry the shopper is shown matches what the ledger
enforces · the agent's 299 tests still pass against the stub.

---

## Phase 6 — The merchant approval screen

**Refs** `P` + the review's "highest impact" gap · **Gate:** a derived profile can be edited
and approved from the browser, and the agent's behaviour changes as a result.

The agent side is **already built** — this is purely the missing UI.
`GET /ingest/report/{merchant_id}` was written for this screen and returns exactly what it
needs; `PUT /ingest/profile/{merchant_id}` takes the edited profile back.

### What the screen renders

From `GET /ingest/report/{merchant_id}`:

- **Header** — derived `category` + `category_confidence`, `derived_by`
  (`bootstrap` | `llm`), `version`, `status` (draft/approved), and `source`
  (filename, sheet, row count).
- **Roles** — which column became id / title / price / stock / image / text, with
  confidence. Flag `roles.missing_required()` loudly: without id, title and price there is no
  checkout, and that is the single most important thing this screen can tell a merchant.
- **Fields table** — one row per column: `column`, `read_as`, `kind`, `values` (first 12),
  `aliases_collapsed`, `unit`/`currency`, `empty_rate`, `unparseable_cells`.
  Editable: `tier` (1/2/3), `layman_name`, `why_it_matters`, `required_before_purchase`,
  `hidden`.
  Show `suggested_required` beside `required_before_purchase` as separate columns —
  the model's opinion and the merchant's decision must never look like the same thing.
- **Cross-field rules** — each proposed rule with its `if`, `then`, `message`, `columns`,
  and an approve / edit / delete control. **Default unapproved.** This is the one part of the
  pipeline that cannot be validated against data, which is why it needs a human signature.
- **Notes** — `profile.notes` carries what the pipeline could not parse or had to guess.

### Submit

`PUT /ingest/profile/{merchant_id}` with `{profile, approved_by, edited_fields, reindex: true}`.
`edited_fields` must list every column the merchant actually touched — `merge.py` uses it to
preserve those edits across a re-ingest, so getting it wrong silently loses their work on the
next upload. Then poll `GET /catalog/status/{merchant_id}` and show reindex progress.

### Files

- `web/merchant-console/index.html` — add a fourth nav item and view alongside the existing
  `catalog` / `activity` / `settings`, following the `data-view` pattern already there.
- `web/merchant-console/review.js` — new, the whole screen.
- `web/merchant-console/app.js` — **rewrite**. It is currently six minified lines holding 342
  generated products, a fake `"Category detected: Home & living"`, and a
  "Confirm and import catalog" button wired to nothing. Replace with real calls:
  `POST {merchant}/catalog/upload` → `POST {agent}/ingest/analyze` → redirect into Review.
- `styles.css` — extend; keep the existing visual language.
- A small `config.js` holding the two base URLs so they aren't scattered through the files.

**Verify:** upload a fixture → analyze → the screen renders real derived fields, not
placeholders · edit a `layman_name` and a tier, flip one `required_before_purchase`, approve
one cross-field rule, submit · `GET /ingest/profile/{id}` shows `status: "approved"` with the
edits intact · re-upload the same catalog and confirm `merge.py` preserved them · confirm in
a chat turn that the agent now uses the approved rule and blocks checkout on the required
field. That last check is the one that proves the screen is wired to behaviour rather than
just to storage.

---

## Phase 7 — Storefront SSE client

**Refs** `P` · **Gate:** a full shop-to-receipt conversation in the browser.

Rewritten against the storefront drop-in recorded above. The UI is now **already built** —
this phase is transport plus de-faking, not construction. Read the drop-in section for what
is real and what is theatre before touching the file.

### 7a. Config and transport

- `web/storefront/config.js` — new, the two base URLs (`AGENT_BASE`, `MERCHANT_BASE`) plus
  `MERCHANT_ID`, so they are in one place across both frontends.
- `POST /chat` consumed as `text/event-stream` via `fetch` + a `ReadableStream` reader —
  **not** `EventSource`, which cannot issue a POST body. Parse `event:`/`data:` frames.
- One `session_id` minted per page load and reused for the whole conversation, including
  the separate confirm stream.

### 7b. Render from typed events only

`token` · `tool_start` · `products` · `comparison` · `probe` · `preview` · `receipt` ·
`error` · `done` — **plus `cart` and `notice`**, which phase 2 found in `agent/events.py`
and the old phase 7 list omitted. `notice` carries the merchant-approved cross-field
warnings from phase 6, so without it the approval screen's rules have no way to reach the
shopper and phase 6 is half-wired.

Never parse prose out of the token stream. That is the contract.

### 7c. De-fake — delete, don't adapt

- Delete `products.csv`, `fallbackProducts`, `parseCsvLine`, `parseProductsCsv`,
  `loadProducts`. The catalog arrives over SSE.
- Delete **both** definitions of `simulate()` (there are two; the second silently wins).
- Delete `showComparisonResults()`'s hardcoded paragraph.
- Delete the card-details step per the decision above — consent, not a PAN, is the
  authorization. Keep delivery details, the Visa panel and the consent checkbox.
- Replace the `headphones`/`buds`/`watch` CSS classes with one neutral placeholder.
  `ProductCard` renders generically from the profile's roles plus an `attributes` dict.
  **No category-specific field may be named in this file** — same rule the agent backend
  holds itself to.
- Currency comes from the server on every amount. Strip the hardcoded `€`/`EUR`, and strip
  `#NV-2042` and `€129.00` from the receipt markup.

### 7d. The trust gate, client side

The `preview` event reveals the payment panel. The consent checkbox enables the confirm
button; pressing it POSTs `/chat/confirm` with `{session_id, preview_id}` and consumes the
**second** SSE stream carrying the `receipt`. Confirmation travels as its own POST and is
never inferred from chat text — the client mirrors the backend's tool-absence gate.

Probe chips render from the `probe` event with `why_it_matters` as the explainer. Show
`filters_relaxed` when present: an honest "I widened the search" is the difference between
a relaxed result and an apparent wrong answer. Show the server's `expires_at` on the
preview, not a client-side countdown.

**Verify:** a full conversation in the browser from vague query through probe, comparison,
cart, preview, confirm, receipt · both fixture catalogs (`power_tools.csv`,
`tea_and_infusions.xlsx`) render correctly through the same code, which is the real test of
7c · `grep -iE "headphone|earbud|watch|screen size|€|EUR" web/storefront/chatbot.js` returns
nothing · the confirm button is the only path to a charge.

---

## Phase 8 — One command brings up the stack

**Refs** `M` `Q` · **Gate:** `docker compose up` gives a working demo from a clean checkout.

- Root `docker-compose.yml`: Postgres + merchant `:8001` + agent `:8002` + payments `:8003`.
  Agent runs **one uvicorn worker, no `--reload`** — its `SessionStore` is in-process and
  carts live in that process.
- Two databases on the one Postgres instance: `payments` and `merchant`.
- Root `.env.example` with every service's vars and the port map.
- Per-service `pyproject.toml` with a real `[project]` table (`M`). Do **not** try to unify
  dependency versions across services — they're separate processes with separate venvs. Pin
  per service.
- **Cross-service smoke test** — `tests/smoke_test.py` at the root: upload → sync → search →
  preview → authorize → confirm against the three real services. Every finding in section 02
  of the review would have been caught by this on the day it was introduced. This is the
  single highest-value artifact of the whole plan.

---

## Appendix A — Contract shapes

### `GET /catalog/raw?merchant_id=eyewear_co&ids=SKU1,SKU2`

```json
{
  "merchant_id": "eyewear_co",
  "id_column": "sku",
  "row_count": 2,
  "rows": [
    { "sku": "SKU1", "Product Name": "…", "RRP inc VAT": "£129.00", "qty_on_hand": "4" }
  ]
}
```

Raw columns, exactly as uploaded. No coercion, no renaming, no normalization. `ids` absent
returns the whole catalog. Every value is a string or `null`.

### `GET /merchants`

```json
{ "merchants": [ { "merchant_id": "eyewear_co", "row_count": 50, "id_column": "sku" } ] }
```

### `POST /payment/preview`

```json
{ "merchant_id": "…", "session_id": "…", "currency": "USD",
  "items": [ { "product_id": "SKU1", "title": "…", "quantity": 1, "unit_price": 129.0 } ] }
```

Response adds `expires_at`. Note `product_id` — the agent currently sends `id`, which is
finding `H`.

### `POST /payment/authorize`

```json
{ "preview_id": "…", "method": "explicit_confirm", "proof": true }
```

---

## Appendix B — Verification gates

| Phase | Block | Gate | Needs |
|---|---|---|---|
| 1 | A | merchant imports; its tests collect; no conflict markers in README | — |
| 2 | A | `docs/CONTRACTS.md` exists and covers both seams | — |
| 3 | A | **299** agent tests pass; payments collects; merchant collects; git shows renames | — |
| 4 | A | `ids=` returns exactly the ids requested; agent indexes off the real merchant | Ollama (already running) |
| ⏸ | — | root `.env` handed over; Docker Desktop started | **you** |
| 5 | B | real charge end to end; receipt shows true total, items and timestamp | Postgres |
| 6 | B | edits survive approval **and** re-ingest; agent behaviour changes accordingly | Postgres · key · Ollama |
| 7 | B | full browser conversation on both fixture catalogs | all of the above |
| 8 | B | `docker compose up` from clean checkout; smoke test green | Docker |

`PROGRESS_LOG.md` gets an entry as each of these closes — not batched at the end.

---

## Appendix C — Resuming after a context reset

If this session runs out of room, a fresh one recovers by reading, in order:

1. **`CLAUDE.md`** — what every service is and where it lives
2. **`IMPLEMENTATION_PLAN.md`** — this file: decisions, phases, contracts
3. **`PROGRESS_LOG.md`** — what is actually done, and the single next action

Then: `git log --oneline -10` and `git status` to confirm the tree matches what the log claims,
and `cd services/agent && uv run pytest -q --tb=line 2>&1 | tail -5` to confirm the baseline is
still green before changing anything.

Do not re-run the review that produced this plan. Its findings are recorded in `CLAUDE.md`
under "Known breakage" and in the artifact linked at the top of this file.

---

## Risks and open items

1. **The agent's 299 tests are the safety net for the whole plan.** They run in ~28s with no
   API key and no network. Run them after every phase, not just phase 3. A drop in the count
   is a regression even if nothing errors.
2. **Phase 4 changes an upload path that the merchant team may be editing concurrently.**
   Raw-row storage is additive — new table, new endpoints, existing behaviour untouched — so
   it should merge cleanly, but coordinate before starting.
3. **Ollama must be running** for embeddings, or `EMBEDDING_PROVIDER=openai` set. A dead
   Ollama degrades `search_catalog` to structured-filter-only rather than hanging, but the
   demo is much weaker without vectors. Decide before phase 6, not during.
4. **Phase 6 depends on phase 4** — the approval screen needs a real profile, which needs a
   real catalog synced off the real merchant. It cannot be usefully built against fixtures
   alone.
5. **`merge.py` and `edited_fields` are the subtle correctness risk in phase 6.** If the
   screen submits the wrong `edited_fields`, merchant edits are silently lost on the next
   re-ingest. Test the re-ingest path explicitly, not just the approve path.
6. **Not doing the `app` package rename** leaves finding `D` formally open. Deleting root
   `app/` removes the hazard that actually bites. Flagged here so it's a recorded decision
   rather than an oversight.
7. **`payments` currently has no `.env.example` entry in any compose file for its Postgres
   credentials** beyond the compose defaults. Phase 8 must not bake `postgres/postgres` into
   anything that could outlive the hackathon.
