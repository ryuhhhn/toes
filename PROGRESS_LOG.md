# Progress log

Append-only execution record for [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
One entry per phase, written **as the phase closes**, never batched.

Resuming after a context reset? Read `CLAUDE.md` → `IMPLEMENTATION_PLAN.md` → this file,
then confirm the tree matches with `git log --oneline -10` and `git status`.
Do not re-run the review that produced the plan.

---

## Baselines — verified 2026-08-29, before any phase ran

| Suite | Command | Result |
|---|---|---|
| Agent | `cd customer/backend && uv run pytest -q --tb=line` | **312 passed** in 33.75s |
| Merchant | `cd merchant/backend && python -m pytest tests -q` | **43 passed** in 0.91s |
| Payments | not yet run — needs Postgres (Block B) | — |

> ⚠️ **Correction to the plan and to `CLAUDE.md`: the agent suite is 312 tests, not 299.**
> Both documents quote 299 from the agent's `PROGRESS.md`, which is stale — the suite has
> grown by 13 tests since it was written. **Phase 3's gate is `312 passed`.** Treat any
> number below 312 as a regression. `CLAUDE.md` gets corrected in the phase 3 doc sweep.

---

## Phase 1 — Stop the bleeding · ✅ complete

**Refs** `A` `B` `C` `N` `O` · closed 2026-08-29

### Files touched

- `.gitignore` — **new** at repo root
- `payments/.gitignore` — appended secrets block
- `app/main.py` → `merchant/backend/main.py` (git rename)
- `tests/` → `merchant/backend/tests/` (git rename, 4 files)
- `app/` — **deleted**: `__init__.py`, `normalize.py` (near-duplicate),
  `schemas.py` + `storage.py` (23-byte `from x import *` shims)
- `merchant/backend/examples.py` — imports repointed
- `merchant/backend/tests/test_api.py`, `tests/test_eyewear_mock_data.py` — imports and
  fixture path
- `README.md` — rewritten

### What changed

1. **The merchant backend can start again** (finding `A`). `main.py` now sits beside the flat
   modules it imports; `from app.normalize` / `app.schemas` / `app.storage` became
   `from normalize` / `schemas` / `storage`. No CWD ambiguity remains — `merchant/backend/`
   is the working directory and everything resolves from there.
2. **Root `app/` is gone** (finding `C`). With it went the third top-level `app` package, so
   the import hazard of finding `D` no longer bites even though the packages were not renamed
   (a recorded decision, not an oversight — see plan risk 6).
3. **Root `tests/` moved** (finding `B`). Their `sys.path.insert(parents[1])` was already
   correct *for the new location* — `parents[1]` from `merchant/backend/tests/` is
   `merchant/backend/`, exactly where `normalize` and `taxonomy` live. No index fix was
   needed. Only `test_api.py`'s `from app.main import app` → `from main import app` changed.
4. **Seed path fixed** (finding, plan step 1.4). `_seed_catalog_from_json()` resolved to
   `toes/eyewear.json` via `parent.parent`; from `merchant/backend/main.py` that would now be
   `merchant/eyewear.json`, still wrong. Now `parent / "eyewear.json"`, which exists. It is
   still the wrong *source* — post-normalization output, useless to the agent — and gets
   repointed at `eyewear_mock_data.csv` in phase 4 as planned.
5. **Root `.gitignore` restored** (finding `N`), covering `.env` first, plus `__pycache__/`,
   `*.py[cod]`, `.venv/`, `.pytest_cache/`, `node_modules/`, `data/`, `*.npy`, `.DS_Store`.
   `payments/.gitignore` got its own secrets block. **This landed before any file containing
   a key is written**, as the plan requires.
6. **README rewritten** (finding `O`). Merge-conflict markers and the React + Vite template
   section are gone; it now carries the fixed port map (8001/8002/8003/9001), real run
   commands per service, test commands with expected counts, and a pointer to the known
   breakage in `CLAUDE.md`.

### Verified

| Check | Command | Result |
|---|---|---|
| Merchant imports | `cd merchant/backend && python -c "import main"` | `OK: app = Merchant Backend` |
| Tests collect | `python -m pytest tests -q --collect-only` | `43 tests collected` |
| Tests pass | `python -m pytest tests -q --tb=line -p no:warnings` | **43 passed** in 0.91s |
| No conflict markers | `grep -n "<<<<<<<\|>>>>>>>" README.md` | no output |
| Agent unaffected | `cd customer/backend && uv run pytest -q --tb=line` | **312 passed** |
| Renames preserved | `git status --short` | `R` for all 5 moved files, not delete+add |

The gate asked only that the tests *collect* (failures were expected to be acceptable).
All 43 pass, so the merchant has a real green baseline going into phase 3.

### Decisions made mid-flight

- **`test_eyewear_mock_data.py` read `pd.read_csv('eyewear_mock_data.csv')` relative to the
  process CWD.** It happened to work only when pytest ran from `merchant/backend/`. Changed
  to `Path(__file__).resolve().parents[1] / 'eyewear_mock_data.csv'`. Not in the plan, but
  the alternative was a test that breaks on invocation directory — and phase 3 moves this
  file, which would have broken it anyway. **Phase 3 must re-point this to
  `services/merchant/fixtures/`** when the CSV moves.
- **`merchant/backend/examples.py` also imported `app.normalize` and `app.main`** — not
  listed in the plan's step 1, found by grep. Repointed with the rest.
- **The agent suite is 312, not 299.** See the correction above.

### Left broken on purpose

- `main.py` still uses the deprecated `@app.on_event("startup")`. Phase 4a replaces the
  whole startup path with a lifespan + lazy config, so changing it now would be rework.
- The merchant still serves `GET /catalog` as a bare list (finding `E`) and has no
  `/catalog/raw`, no `/merchants`, no CORS. All phase 4.
- `eyewear.json` is still the seed source. Phase 4d.
- `DATABASE_URL` is still captured at import time in `database.py`. Phase 4a.

### Next action

**Phase 2** — write `docs/CONTRACTS.md` and `docs/ARCHITECTURE.md`. No code changes.
Nothing external needed.

---

## Phase 2 — Write the contracts down · ✅ complete

**Refs** `E` `F` `G` `H` `I` `J` `K` `L` · closed 2026-08-29 · no code changed

### Files touched

- `docs/CONTRACTS.md` — **new**, 15.5 KB, the cross-team source of truth
- `docs/ARCHITECTURE.md` — **new**, 10.2 KB

### What the contracts say

Written from the **actual source files**, not from the review's summary — every shape below
was read out of the code before being written down.

`docs/CONTRACTS.md`:

- **§0 The normalization boundary** — the ownership table. Merchant stores and serves raw
  rows; the agent derives all meaning. Merchant `normalize`/`taxonomy`/`llm_client` are
  console-display only and must never sit on the agent's path. `GET /catalog/search` retired.
- **§1 Merchant → Agent** — `GET /catalog/raw` (`ids=` filtering, raw column names, every
  value a string or null), `GET /merchants`, `GET /health` with the storage mode,
  the upload notification, CORS.
- **§2 Agent → Payments** — all three endpoints plus the receipt, each with a table of what
  the agent sends against what payments expects.
- **§3 SSE events** — all eleven types, `ProductCard`'s generic shape, the rule that
  confirmation is its own POST, and the no-hardcoded-field rule restated for the frontend.
- **§4 Audit authority** (`L`) — ledger authoritative for money, `audit.jsonl` for what the
  agent did, joined on `preview_id`, ledger wins any money disagreement.
- **§5 Change protocol** — contract first, then stub, then code.

`docs/ARCHITECTURE.md` lifts the four roles, both flow diagrams, the profile's purpose and
the eight trust invariants out of the agent's `CLAUDE.md`, which owned them by accident.

### Findings confirmed by reading the code — beyond what the review recorded

Reading the payments router and schemas against the agent's clients turned up **four details
the plan does not mention.** All four are now in CONTRACTS and matter for phase 5:

1. **`POST /payment/confirm` already works.** The agent sends
   `{preview_id, authorization_id, session_id}`; payments' `ConfirmRequest` needs only the
   first two and pydantic ignores the extra. So confirm is *not* one of the 422s — only
   preview and authorize are. Phase 5 drops `session_id` for honesty, not to fix a break.
2. **`currency` on preview does not 422 either** — payments' `Cart` has no `currency` field,
   so it is silently dropped and the response hardcodes `"USD"`. A wrong-currency bug that
   fails quietly, which is worse than the 422s that fail loudly.
3. **The error envelopes differ in kind** (not in the review). The stub raises
   `detail={"code","message"}`; real payments raises `detail="Preview not found"` — a plain
   string. The agent's `_decode_error` handles both, but a string collapses to the generic
   code `payment_failed`, so **against real payments the agent loses every specific failure
   code.** A declined card and an expired preview become indistinguishable, and they need
   different conversational responses. Recorded as CONTRACTS §2.5; phase 5 should move
   payments to the `{code, message}` envelope.
4. **`?fail=` is a silent no-op against real payments.** FastAPI ignores unknown query
   params, so the agent's failure-injection parameter passes through and a "declined card"
   demo simply succeeds. CONTRACTS §2.6.

Also recorded: payments has **no `tax`** by design (`total == subtotal`), so the agent's
`PreviewEvent.tax` will be `0.0` against real payments and non-zero against the stub. Not a
bug — but the shopper-facing total must come from the server, never be recomputed.

The SSE contract has **eleven** event types, not the nine the plan's phase 7 lists —
`cart` and `notice` are also defined in `agent/events.py`. `notice` matters: it is the
carrier for merchant-approved cross-field warnings, so phase 7's storefront must render it
or approved rules from phase 6 will have no way to reach the shopper.

### Verified

| Check | Result |
|---|---|
| `docs/CONTRACTS.md` exists, covers both seams | §1 merchant seam, §2 payments seam, §3 frontend seam |
| `docs/ARCHITECTURE.md` exists | 7 sections |
| Shapes read from source, not assumed | `payments/app/models/schemas.py`, `payments/app/routers/payment_router.py`, `customer/backend/app/clients/{merchant,payment}.py`, `customer/backend/app/agent/events.py`, `customer/backend/stubs/mock_services.py` |

No code changed, so no suite was re-run — phase 1's counts (312 agent / 43 merchant) stand.

### Decisions made mid-flight

- **`docs/CONTRACTS.md` documents `/catalog/raw` as the contract**, while `GET /catalog`
  stays as the merchant's own normalized view for its console. The plan's phase 4c said
  "retire `GET /catalog/search`" but was silent on `/catalog` itself. Keeping it is the
  smaller change and it is genuinely useful to the merchant console; the contract just states
  plainly that the agent must never read it.
- **`docs/ARCHITECTURE.md` links to `docs/agent/CLAUDE.md`**, which does not exist until
  phase 3 moves it there. A deliberate forward reference — phase 3 makes it resolve.

### Left broken on purpose

Everything from phase 1's list still stands. Phase 2 is documentation only: every ❌ in
CONTRACTS is still a live mismatch in the code, scheduled for phase 4 (merchant seam) or
phase 5 (payments seam).

### Next action

**Phase 3 — the move.** Pure `git mv` into `services/{merchant,agent,payments}` + `web/` +
`docs/agent/`, zero logic changes, one commit. Gate: **312** agent tests (not 299), payments
collects, merchant collects, and `git status` shows renames rather than delete+add.

---

## Phase 3 — The move · ✅ complete

**Refs** target layout · closed 2026-08-29

### Files touched

128 git renames, 13 additions, 6 deletions, 1 modification.

| Move | From → To |
|---|---|
| merchant | `merchant/backend/` → `services/merchant/`, then flat modules → `services/merchant/app/` |
| agent | `customer/backend/` → `services/agent/` (inner `app/` untouched) |
| payments | `payments/` → `services/payments/` |
| console | `merchant/frontend/` → `web/merchant-console/` |
| storefront | `customer/frontend/` → `web/storefront/` |
| agent docs | 4 `.md` files → `docs/agent/` |
| merchant doc | `LLM_AND_TESTING_GUIDE.md` → `docs/merchant-llm-and-testing.md` |
| compose | `payments/docker-compose.yml` → `docker-compose.yml` (root) |
| fixtures | `eyewear_mock_data.csv`, `eyewear.json` → `services/merchant/fixtures/` |

New: `services/merchant/pyproject.toml`, `services/payments/pyproject.toml` (real `[project]`
table, finding `M`), `services/merchant/app/__init__.py`, `app/db/__init__.py`,
`services/agent/README.md` (the pointer to the moved docs).
Rewritten: `CLAUDE.md` (every path changed), `README.md` (paths + test counts).
`merchant/` and `customer/` are gone; empty parents removed.

### Verified

| Check | Command | Result |
|---|---|---|
| Agent | `cd services/agent && uv run pytest -q --tb=line` | **312 passed** in 28.29s |
| Merchant | `cd services/merchant && python -m pytest -q --tb=line` | **43 passed** in 0.93s |
| Payments collects | `cd services/payments && uv run pytest -q --collect-only` | **7 collected** |
| Merchant imports | `python -c "from app.main import app"` | `OK: Merchant Backend` |
| Renames not delete+add | `git status --short` | 128 `R`, 6 `D` (all intended) |
| No secrets staged | `git diff --cached` scan for `sk-`/`.env`/`.venv`/`data/`/`.npy` | only `.env.example` placeholders (`sk-...`) |

The 6 deletions are the four root `app/` files from phase 1, plus `payments/.gitignore` and
`payments/pyproject.toml` — those two show as delete+add rather than rename because their
content was rewritten, not moved. Intended and accounted for.

`uv` rebuilt the agent's moved `.venv` automatically on first `uv run`; no re-sync was needed
and the existing `data/` indexes came along with the directory rename, so **no reindex is
required.**

### Decisions made mid-flight

1. **The merchant's flat→`app/` restructure was done here, in phase 3, not deferred to
   phase 4.** The plan called phase 3 "pure `git mv`, zero logic changes", and repointing
   `from normalize import` → `from app.normalize import` is technically a logic change. But
   phase 3's *stated purpose* is that "phases 4–7 are written in final locations", and its
   rationale for one commit is "so everyone rebases exactly once". Leaving the merchant flat
   would have forced a second structural move in phase 4 and a second rebase. The restructure
   is mechanical, fully covered by the 43 tests, and they pass. **Phase 4 is now purely
   feature work** — `app/config.py`, `app/api/catalog.py` and the `raw_rows` table land in
   locations that already exist.
2. **`app/db/database.py`**, not `app/database.py` — matches the plan's target layout, so
   phase 4a's "+ raw_rows table" edits a file already in its final home.
3. **`examples.py` stayed at `services/merchant/examples.py`**, outside the `app/` package.
   It is a standalone demo runner, not part of the service. Imports repointed.
4. **A `pyproject.toml` for the merchant was written now rather than in phase 8.** With the
   `app/` package, tests need the service root on `sys.path`; `pythonpath = ["."]` is the
   clean way to get it, and it replaces `requirements.txt` as the target layout specifies.
   `requirements.txt` is kept for now — nothing reads it, but removing it is phase 8's call.
5. **A `.venv` was created for `services/payments`** (`uv venv` + `uv pip install -e ".[dev]"`).
   Payments could not even *collect* without `asyncpg`, which is not installed system-wide, so
   the phase 3 gate was unreachable otherwise. This installs local packages only — no Docker,
   no keys, no spend — so it stays within Block A's "needs nothing new". It also front-loads
   setup that Block B needed anyway. Produced `services/payments/uv.lock`.
6. **Nothing has been committed.** The plan asks for phase 3 to be one commit; the working
   tree is currently *staged* (phases 1–3 in the index) but uncommitted, pending a decision
   on branch-vs-main. See "Open questions" below.

### Discovered — matters for the pause gate

**A native PostgreSQL 18 Windows service (`postgresql-x64-18`, PID 8652) is running and
holding port 5432.** Found because payments' tests failed with
`asyncpg.exceptions.InvalidPasswordError` rather than connection-refused — something is
listening, and it is not ours.

`docker compose up -d db` as currently written publishes `5432:5432` and **will fail to bind.**
Three ways out, and it is the user's call:

| Option | Effect |
|---|---|
| Stop the Windows service | Docker takes 5432; `.env` unchanged. Affects anything else using local Postgres. |
| Publish the container on **5433** | `ports: "5433:5432"`; host `DATABASE_URL` uses 5433. Nothing else touched. Inside compose, services still reach `db:5432` on the internal network, so only bare `uvicorn` runs care. |
| Use the native Postgres, drop the db container | Needs its superuser password and manual `payments` + `merchant` database creation. |

This decides the `DATABASE_URL` values written into the root `.env` in phase 4f, so it is
answered **before** phase 4f, not at the gate.

### Left broken on purpose

Everything in the "Still open" list of `CLAUDE.md` — findings `E`–`L`, `P`, `Q`. Phase 3 moved
code; it fixed no contract. Payments' 7 tests still error on the database connection, which is
correct: Postgres is a Block B dependency.

### Next action

**Phase 4 — merchant raw rows and the catalog contract.** Config with lazy `DATABASE_URL`,
the `raw_rows` table, `GET /catalog/raw` with `ids=` filtering, `GET /merchants`, CORS, the
CSV seed, the agent client repoint, and the unified `.env`. Gate: the agent syncs a real
catalog off the real merchant with `use_llm=false`.

---

## Phase 4 — Merchant: raw rows and the catalog contract · ✅ complete

**Refs** `E` `F` `G` `J` `K` + the storage verdict · closed 2026-08-29

### Files touched

**New:** `services/merchant/app/config.py` · `services/merchant/app/api/__init__.py` ·
`services/merchant/app/api/catalog.py` · `services/merchant/tests/conftest.py` ·
`.env.example` (root) · `.env` (root, gitignored) · `docker/initdb/01-create-merchant-db.sql`

**Rewritten:** `services/merchant/app/db/database.py` · `services/merchant/app/main.py` ·
`docker-compose.yml`

**Edited:** `services/merchant/app/storage.py` · `services/merchant/tests/test_api.py` ·
`services/agent/app/clients/merchant.py` · `services/agent/app/config.py` ·
`services/agent/stubs/mock_services.py` · `services/agent/.env`

### What changed

**4a — config and storage mode.** `app/config.py` holds all env access.
`MERCHANT_DATABASE_URL` is preferred with `DATABASE_URL` as fallback, because the root
`.env` carries a bare `DATABASE_URL` for payments pointing at a *different* database on the
same instance. The engine is built in the lifespan via `init_engine()`, never at import, so
finding `J` is gone: setting the variable late now works. `_use_sql()` resolves from the
engine instead of re-reading `os.environ`. `GET /health` returns
`{"status","storage","merchants"}` and the mode is logged at startup with the password
redacted.

**4b — raw rows.** New `raw_rows` table (`merchant_id` PK, `id_column`, `rows` JSON,
`row_count`, `uploaded_at`, `source_filename`), one row per merchant, replace-on-upload.
`replace_raw_rows` / `get_raw_rows` / `list_merchants` / `first_unique_column`, each with an
in-memory branch beside the SQL one. `_stringify` enforces the contract that every value is
a string or `null` — the agent's coerce step needs the original text, not a parsed float, to
find currency and units. `POST /catalog/upload` stores raw rows **before** normalizing, and
stores them even when normalization returns 422, so a sheet the console rejects is still
usable by the agent.

**4c — endpoints.** `GET /catalog/raw?merchant_id=&ids=` and `GET /merchants` per
CONTRACTS §1.1/§1.2. `GET /catalog/search` retired. `CORSMiddleware` added. Routes lifted
out of `main.py` into `app/api/catalog.py`, split by audience with the reason stated in the
module docstring.

**4d — seed.** `_seed_catalog_from_json` replaced by `_seed_catalog`, reading
`fixtures/eyewear_mock_data.csv` as `eyewear_co` and storing raw rows *then* normalized
products. `eyewear.json` stays as a normalization-test fixture. Finding `K` closed.

**4e — agent side.** `fetch_catalog` and `fetch_by_ids` now call `/catalog/raw`. The stub
serves `/catalog/raw` too (CONTRACTS §5: the stub is a contract implementation, not a
convenience), with `/catalog` kept as an alias.

**4f — unified `.env`.** Root `.env.example` covers all four services with the port map and
inline notes. Root `.env` generated from it, carrying 16 values over from
`services/agent/.env` including the 164-char OpenAI key.

### Verified — against the REAL merchant and the REAL agent, not the stub

Merchant on `:8001` (in-memory), agent on `:8002`, Ollama live. Both since shut down.

| Gate check | Result |
|---|---|
| Upload `eyewear_mock_data.csv` to the real merchant | `ok: True`, 50 in / 50 out |
| `ids=EYE-1002,EYE-1007` returns **exactly two rows** (finding `F`) | `row_count: 2`, ids `['EYE-1002','EYE-1007']` |
| Raw columns survive byte-for-byte | 18/18 column names identical **and in order**; **900/900 cells** identical to the uploaded file |
| Untouched values | `Price (USD)` to `"239.03"`, `Size (Lens-Bridge-Temple mm)` to `"48-16-135"` — strings, uncoerced |
| Agent `/health` reaches the real merchant | `merchant: {ok: true, url: http://localhost:8001}` |
| `POST /ingest/analyze` with `use_llm=false` | `derived_by: bootstrap`, 16 fields from 18 columns, **no spend** |
| Roles derived from arbitrary column names | id=`Product Code` (1.00), title=`Product Name` (1.00), price=`Price (USD)` (0.74), stock=`Stock Quantity` (0.95) |
| `POST /catalog/sync/eyewear_live` | `built: true`, 50 rows, 50 descriptors, `ollama:nomic-embed-text`, **dim 768** |
| `reverify_cart` against a stock-zeroed row | "Item 1002 has just gone out of stock." — and the price-move path also fired: "The price of Item 1007 changed from 0 to 180.06.", corrected in place |
| `GET /catalog/raw` unknown merchant | 404, not an empty catalog |
| `GET /health` | `{"status":"ok","storage":"memory","merchants":1}` |

Suites: **merchant 47 passed** · **agent 312 passed** · **payments 7 collected**.

> **Merchant count moved 43 to 47, and that is not a regression.** Removed: 5
> `TestSearchOperations` tests and `test_search_without_merchant_id`, all covering the
> retired `/catalog/search`. Added: 8 `TestRawRowContract` tests (verbatim columns,
> uncoerced values, id detection, `ids=` filtering, unknown-id handling, 404, `/merchants`,
> health mode) plus `test_raw_without_merchant_id` and `test_search_endpoint_is_retired`.
> 43 − 6 + 10 = 47.

### Decisions made mid-flight

1. **The plan's `env_file` tuple order is backwards, and I verified it rather than trusting
   either of us.** The plan specifies `env_file=(".env", "../../.env")` and states that a
   service-local `.env` wins. **pydantic-settings gives precedence to the LAST file in the
   tuple**, so that order makes the *root* override the service — the opposite of the intent.
   Proven with a two-file experiment before writing any config:

   | tuple | resolved value |
   |---|---|
   | `(".env", "../.env")` — the plan's order | `who=root` ❌ |
   | `("../.env", ".env")` — corrected | `who=local` ✅ |

   Both configs use `("../../.env", ".env")`. Root remains a genuine fallback: a key present
   only in the root still resolves. **This was the one detail flagged in advance as the thing
   that would have bitten us, and the planned fix had the order inverted** — it would have
   silently broken every service-local override.

2. **`services/agent/.env` had `MERCHANT_BASE_URL`/`PAYMENT_BASE_URL` pinned to the stub
   (`:9001`).** Because service-local correctly wins, the new root `.env` would have had no
   effect on exactly the two settings that switch the agent onto the real services — it would
   have looked configured and silently stayed on stubs. Both lines are now commented out in
   place, with a note explaining how to re-enable stub mode. Verified: the agent resolves
   `8001`/`8003` from the root while local precedence still works everywhere else.

3. **A merchant `tests/conftest.py` was required, and its absence was a real regression I
   introduced.** Once the merchant read the root `.env`, its tests inherited
   `MERCHANT_DATABASE_URL` and tried to reach Postgres: **14 failed, 12 errored.** A suite
   that passes or fails depending on whether a container happens to be running is not a
   suite. The conftest pins the DB vars to empty strings (os.environ beats dotenv files) and
   clears both caches. Verified hermetic even with `MERCHANT_DATABASE_URL` exported in the
   shell: 47 passed. The agent's conftest already did the equivalent, which is why its 312
   never wobbled.

4. **An unreachable-but-configured database is now a hard startup failure, not a silent
   fallback to memory.** Found live: with the root `.env` set and Docker down, the merchant
   died on a raw SQLAlchemy traceback. Falling back to memory would recreate the exact
   "debugging a store you aren't using" problem this phase exists to kill, so the lifespan
   raises a message naming the URL (password redacted) and both ways out. The in-memory store
   remains the mode when no database is configured at all.

5. **`docker-compose.yml` gained the `merchant` database now, not in phase 8.** The `.env`
   handed over references `.../merchant`, and handing over a config whose values do not
   resolve is how the current mismatches happened. Added
   `docker/initdb/01-create-merchant-db.sql`, a named `pgdata` volume, and a healthcheck.
   Port stays `5432:5432` per the decision recorded at the phase 3 gate.

6. **`GET /catalog` was kept** as the console's normalized view (CONTRACTS §1.3); only
   `/catalog/search` was retired. `/catalog/search` now returns 405 rather than 404, because
   `PATCH /catalog/{product_id}` still matches the path — the test accepts either.

### Left broken on purpose

- **The payments seam entirely** — findings `H` `I`, plus the error-envelope and `?fail=`
  divergences found in phase 2. All phase 5, all Block B.
- **Neither frontend calls a backend** (`P`) — phases 6–7.
- Merchant `requirements.txt` still exists beside `pyproject.toml`; nothing reads it.
- Payments' 7 tests still error without Postgres. Correct — Block B.
- `storage.search_catalog()` remains in the module though no route calls it. Harmless, and
  the console may want it; removing it is phase 8's call.

### Next action

**⏸ THE PAUSE GATE.** Block A is complete and verified. Nothing from Block B is written.
See the handover below.

---

# ⏸ PAUSE GATE — Block A complete, handed back

**Status: phases 1–4 done and verified. Nothing from Block B (phases 5–8) has been written.**

### Test counts at the gate

| Suite | Command | Result |
|---|---|---|
| Merchant | `cd services/merchant && uv run pytest -q` | **47 passed** |
| Agent | `cd services/agent && uv run pytest -q --tb=line` | **312 passed** |
| Payments | `cd services/payments && uv run pytest -q` | **7 collected**, all erroring on the DB connection until Postgres is up |

### The root `.env` — what is in it

Generated at the repo root, gitignored, **carrying your existing OpenAI key** (164 chars,
`sk-proj…`) across from `services/agent/.env`.

Carried over unchanged (16): `LLM_PROVIDER` `OPENAI_API_KEY` `OPENAI_MODEL`
`ANTHROPIC_MODEL` `EMBEDDING_PROVIDER` `OLLAMA_BASE_URL` `OLLAMA_EMBED_MODEL`
`OPENAI_EMBED_MODEL` `EMBED_TIMEOUT_SECONDS` `MAX_PROBES_PER_TURN`
`MAX_PROBES_PER_SESSION` `MAX_TOOL_ROUNDS` `SEARCH_MIN_RESULTS` `SEARCH_TOP_K`
`PREVIEW_TTL_SECONDS` `DATA_DIR`

Set by this phase: `MERCHANT_BASE_URL=http://localhost:8001` ·
`PAYMENT_BASE_URL=http://localhost:8003` ·
`DATABASE_URL=postgresql://postgres:postgres@localhost:5432/payments` ·
`MERCHANT_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/merchant` ·
`POSTGRES_USER` · `POSTGRES_PASSWORD` · `SEED_ON_STARTUP=true` · `CORS_ORIGINS=*` ·
`VISA_MODE=mock` · `PAYMENT_TTL_MINUTES=15` · `LLM_TIMEOUT_SECONDS=90`

**Still blank — one only:** `ANTHROPIC_API_KEY`. Nothing needs it while
`LLM_PROVIDER=openai`. Fill it only to flip providers.

### What you need to do

1. **Stop the native Postgres so Docker can bind 5432** (your decision at the phase 3 gate).
   In an **admin** PowerShell:
   ```
   Stop-Service postgresql-x64-18
   ```
   It is currently running as PID 8652 and holds the port. Without this,
   `docker compose up -d db` fails to bind.
2. **Start Docker Desktop**, then from the repo root:
   ```
   docker compose up -d db
   ```
   This creates both databases — `payments` from `POSTGRES_DB`, and `merchant` from
   `docker/initdb/01-create-merchant-db.sql`.
3. **Skim the root `.env`** and correct anything you want different.
4. Tell me to continue.

### Then I resume at phase 5

Running `scripts/live_check.py` once (~$0.14 of real spend) to confirm the environment
before touching payment code, then closing the payments seam.

### Two things to know before Block B

- **Nothing is committed.** Phases 1–3 are staged in the index; phase 4 is unstaged. Your
  call at the phase 3 gate. `git status` reads cleanly as staged=structure,
  unstaged=phase 4.
- **The merchant now refuses to start if `MERCHANT_DATABASE_URL` is set and unreachable**,
  rather than silently using memory. So step 1 and 2 above are required before the merchant
  will boot with the root `.env` as written. To run it without Postgres, clear
  `MERCHANT_DATABASE_URL` — `GET /health` will report `"storage": "memory"`.

---

## Storefront drop-in + DB unblock · ✅ complete

Closed 2026-08-29, at the resume from the pause gate. Two things that had to land before
phase 5 could be verified against anything real.

### The database was not actually reachable — and reported that it was

`docker compose ps` said `toes-db-1 … Up (healthy) 0.0.0.0:5432->5432/tcp`, and the container
genuinely had both databases with the right password. Payments still failed with
`InvalidPasswordError`.

**Cause:** the native `postgresql-x64-18` service (PID 8652) was still running and holding
5432 on `0.0.0.0` and `::`. On Windows that **shadows** a container's port publish rather
than failing it — Docker reports the binding, and host connections reach the native server.
The plan's phase 3 gate anticipated a bind *failure*; the real failure mode is quieter and
worse, because every diagnostic points at the container.

**Resolved by publishing on 5433** rather than stopping the service — no admin rights needed
and nothing else using the local Postgres is disturbed.

- `docker-compose.yml` — `"${DB_HOST_PORT:-5433}:5432"`, with the shadowing explained
- `.env` / `.env.example` — `DB_HOST_PORT=5433`, both DSNs moved to 5433
- **`services/payments/app/config.py` — new.** Payments read raw `os.environ` in three
  modules and **never loaded the root `.env` at all**, so it defaulted to
  `localhost:5432/payments` regardless of what the root file said. All env access now goes
  through pydantic-settings with the `("../../.env", ".env")` order phase 4 verified.
- `services/payments/tests/conftest.py` — `ADMIN_DSN`/`TEST_DSN` hardcoded
  `localhost:5432`, so the suite tested against whatever owned that port rather than the
  configured server. Now derived from config.

**Payments' 7 tests passed for the first time.**

### The storefront was replaced

A new customer frontend was dropped at `customer-frontend/customer/frontend/` and moved to
`web/storefront/`; `customer-frontend/` deleted. 1,155 formatted lines replacing 19 minified
ones, bringing the whole post-cart UI the old mockup lacked — order panel with quantity
steppers, delivery details, a Visa Secure panel, an explicit consent checkbox, a receipt card.

Corrected on the way in:

- Its `serve.py` defaulted to **port 8002, the agent's port**. Promoted to `web/serve.py` on
  **:8080**, serving the whole `web/` tree so both frontends share one origin.
- `chatbot.html` linked to a sibling `index.html` that does not exist there; repointed at
  `../merchant-console/index.html`.

`IMPLEMENTATION_PLAN.md` gained a "Storefront drop-in" section recording what the file gives
us free, what is theatre, and **three problems that are not merely "unwired"**: a duplicated
`simulate()`, hardcoded product categories that violate the category-agnostic rule, and a
card-details form collecting a PAN that payments has no field for. Phase 7 was rewritten
against the real file.

---

## Phase 5 — Payments: close the money contract · ✅ complete

**Refs** `H` `I` + the envelope and `?fail=` divergences found in phase 2 · closed 2026-08-29

### Files touched

**Payments:** `app/config.py` (new) · `app/models/schemas.py` · `app/routers/payment_router.py` ·
`app/services/visa_service.py` · `app/services/ledger_service.py` · `app/db/database.py` ·
`tests/conftest.py` · `tests/test_payment_contract.py` (new, 18 tests)

**Agent:** `app/clients/payment.py` · `app/tools/confirm_and_pay.py` ·
`app/tools/preview_transaction.py` · `app/session/models.py` · `app/api/chat.py` ·
`stubs/mock_services.py`

**Docs:** `docs/CONTRACTS.md` §2.1–2.7, all moved ❌/🚧 → ✅

### What changed

**Payments side.** `Cart.currency` (default `"USD"`) replaces the hardcoded literal.
`TransactionPreview.expires_at` is a **pydantic computed field over `created_at`**, not a
stored column — so it cannot drift from the TTL `is_expired` enforces. One clock, no
migration. Every error became `detail={"code", "message"}`, and on the confirm path the wire
code is literally the same string the ledger records as its `confirm_blocked` reason, so the
audit trail and the agent cannot disagree about what went wrong. `?fail=` added to all three
endpoints.

**The stub was converged to the real dialect, not the other way round** (CONTRACTS §5:
contract → stub → code). `PreviewItem.id` → `product_id`; `AuthorizeRequest` → `{preview_id,
method, proof}`; and confirm now returns the real `Transaction` shape instead of a fat
receipt. **That fat receipt was the root cause** — it let the agent read `total`, `tax`,
`items` and `timestamp` against a stub for months, none of which exist in production.

**Agent side.** Item key `id` → `product_id`. Authorize body → `{preview_id,
method: "explicit_confirm", proof: true}`. `confirm_and_pay` reads `amount`/`created_at` and
builds line items from `session.active_preview.items`. `receipt()` unwraps `ReceiptView`.
`new_preview` takes the server's `expires_at`, with `PREVIEW_TTL_SECONDS` demoted to a
fallback.

### Verified — against REAL payments and REAL Postgres, not the stub

| Gate check | Result |
|---|---|
| Full conversation → charge | `RECEIPT txn=740bc38e… total=180.06 USD` |
| Receipt line items (were empty) | `Maui Jim Elite MAU-868 x1 @ 180.06` |
| Receipt timestamp (was blank) | `2026-08-29T12:12:23.568603Z` |
| Currency honoured (was hardcoded USD) | EUR cart charged **240.0 EUR**, row present in the ledger |
| Expiry the shopper sees | `12:27:19` for a `12:12:19` preview = **15 min**, the ledger's TTL, not the agent's 5 |
| Ledger timeline | `preview_created → auth_granted → charge_attempted → charge_succeeded` |
| Declined card, conversational | typed `error {code: card_declined}`, agent offered an alternative, **cart intact**, 0 transactions persisted |
| Merchant storage mode | `{"storage":"postgres"}` — **first run ever on real Postgres** |
| Agent `/health` | all four checks ok: llm, embeddings, merchant, payment |
| Suites | agent **312** · payments **25** · merchant **47** |

### Decisions made mid-flight

1. **The stub's confirm response was converged down to the real `Transaction` shape.** The
   plan said only to fix the agent's read side. But the stub returning a richer receipt than
   production is *why* the agent read four non-existent fields; leaving that asymmetry means
   the 312 tests keep validating a dialect nobody speaks. No test asserted on receipt fields,
   so this was free. **A receipt's line items now come from the preview the shopper
   authorised** — the honest source regardless of what payments echoes back.
2. **`?fail=` had no route into a conversation, so the demo beat was unreachable.** The
   plan's gate requires "a declined card leaves the cart intact and the agent offers an
   alternative", but nothing threaded `fail` from a request through to the client, and the
   mock decline rule needs a total ending in `.99` — **no row in the eyewear catalogue
   produces one at any quantity, which I checked rather than assumed.** Added an optional
   `fail` to `POST /chat/confirm`, stored on the session, applied at capture. It can only
   turn a success into a failure, never skip a gate.
3. **The injection is applied at capture, not at authorize.** Injecting at authorize returned
   a bare HTTP 402 from `/chat/confirm` — correct, and useless: the agent never entered the
   loop and never spoke. Authorize records consent; it is not a card check. Moved to capture,
   the decline arrives as a typed `error` event and the agent offers an alternative.
4. **And then injected *inside* `charge_card`, not at the router door.** Short-circuiting at
   the door produced a "decline" with `preview_created → auth_granted` and nothing after it —
   a declined charge the ledger had never heard of, in the one service whose entire purpose is
   that the ledger knows. It now writes `charge_attempted → charge_failed` like a real one.
5. **The confirmation token now expires with its preview** rather than on its own
   `PREVIEW_TTL_SECONDS` clock. A token outliving the thing it consents to is meaningless,
   and payments would refuse the charge anyway.
6. **A stale uvicorn worker cost real debugging time and is worth recording.** Backgrounded
   uvicorn on Windows does not die from `pkill` or `kill %1` in this shell; the "restarted"
   agent silently failed to bind and the *old* process kept serving, so a code change appeared
   to have no effect. Kill by PID: `Get-NetTCPConnection -LocalPort <p>` → `Stop-Process`.
   Relevant to the demo too — **the agent must never be restarted mid-demo**, because
   `SessionStore` is in-process.

### Left broken on purpose

- **Neither frontend calls a backend** (`P`). Phases 6–7 — next.
- Payments has no `tax`, so `PreviewEvent.tax` is `0.0` against real payments and non-zero
  against the stub. By design, recorded in CONTRACTS §2.1.
- `PREVIEW_TTL_SECONDS` still exists as a fallback. Intentional.
- Merchant `requirements.txt` still sits beside `pyproject.toml`; nothing reads it. Phase 8.

### Next action

**Phase 6 — the merchant approval screen.** The agent side is already built
(`GET /ingest/report/{merchant_id}`, `PUT /ingest/profile/{merchant_id}`); this is the
missing UI, plus rewriting `web/merchant-console/app.js`, currently six minified lines with
342 hardcoded products.

---

## Phase 6 — The merchant approval screen · ✅ complete

**Refs** `P` + the review's "highest impact" gap · closed 2026-08-29

### Files touched

**New:** `web/config.js` (shared by both frontends) · `web/merchant-console/review.js`

**Rewritten:** `web/merchant-console/app.js` — was six minified lines holding 342 generated
products, a hardcoded `"Category detected: Home & living"`, and an import button wired to
nothing.

**Edited:** `web/merchant-console/index.html` (fourth nav item + review view, de-faked stat
cards) · `web/merchant-console/styles.css` (+62 lines, same visual language) ·
`services/agent/app/agent/policy.py` · `services/agent/app/agent/loop.py` ·
`services/agent/pyproject.toml` · `services/agent/tests/test_trust_gate.py`

### What the screen does

Two documents, deliberately. `GET /ingest/report/{id}` is the **human view** and carries
values the server already derived for display (`read_as`, `aliases_collapsed`, role
confidences) that would otherwise be recomputed in JS, badly. `GET /ingest/profile/{id}` is
the **machine object** that gets edited and PUT back. They join on column name, so the screen
never reconstructs a profile out of display strings.

Renders: the derived category with confidence and whether a model or only rules produced it;
a **loud banner when id/title/price are missing**, because without them there is no checkout
and that is the most important thing this screen can say; the role table with per-role
confidence banded green/amber/red; every column with its sampled values, empty rate,
unreadable-cell count and merged spellings; and editable `tier`, `layman_name`,
`why_it_matters`, `required_before_purchase`, `hidden`. **`suggested` and `required` are
separate columns** — the model's opinion and the merchant's decision must never look like the
same thing. Cross-field rules are **unapproved by default**; an unticked rule is simply not
submitted.

`edited_fields` is tracked per control and sent on submit, because `merge.py` preserves
exactly those columns across a re-ingest — getting the list wrong silently loses the
merchant's work on their next upload.

### The finding this phase existed to catch

The plan's gate ends: *"confirm in a chat turn that the agent now uses the approved rule and
blocks checkout on the required field. That last check is the one that proves the screen is
wired to behaviour rather than just to storage."*

It was not. **`required_before_purchase` was enforced only by the system prompt.**
`policy.can_checkout()` and `policy.blocking_gaps()` both existed and were correct, but
`available_tools()` never called them — `preview_allowed(session)` checked only that the cart
was non-empty. Verified live before fixing: with Brand marked required, a shopper saying
*"buy me any pair of sunglasses right now, I'll take the first one"* got a **preview and a
total**.

That directly contradicts the module's own opening line — *"a prompt instruction is not a
gate; tool absence is"*. Fixed by threading `profile` into `available_tools()` /
`preview_allowed()` and withdrawing `preview_transaction` from the schema while a required
field is unsettled. `profile` is optional so callers without one keep the cart-only rule.

### The test suite was validating a different repository

Found while chasing why the policy change appeared to have no effect: a test asserting the
new two-argument signature failed with `TypeError: takes 1 positional argument`, while the
same call worked outside pytest.

**`services/agent/.venv/Scripts/pytest.exe` and `uvicorn.exe` were stale wrappers with
`C:\Users\colin\Downloads\visa_agent_backend\.venv\Scripts\python.exe` baked in** — a
separate, older copy of this service elsewhere on disk, whose own editable install put
`visa_agent_backend` on `sys.path`. So `uv run pytest` imported `app` from **that** tree.

- The **live servers were unaffected**: uvicorn inserts the CWD at the front of `sys.path`,
  so every end-to-end check in phases 4 and 5 genuinely exercised this repo's code. Only the
  test suite was misdirected.
- The agent's `pyproject.toml` had **no `pythonpath` setting**, unlike merchant and payments,
  which is why nothing put the service root ahead of the stray interpreter's site-packages.

Fixed by deleting and rebuilding `.venv` (**`uv sync --all-extras`** — plain `uv sync` skips
the `dev` extra that holds pytest, which is how the stale wrapper kept getting used) and by
adding `pythonpath = ["."]` to the agent's pytest config so the source is authoritative no
matter which interpreter starts pytest.

> ⚠️ **The agent suite is 316, not 312.** The old 312 was measured against
> `visa_agent_backend`. Rebuilding surfaced 10 genuine failures — all from phase 5's
> `authorize()` signature change, in a `tests/test_trust_gate.py` helper still passing
> `session_id`. Fixed, plus 4 new tests for the required-field gate. **316 is the number
> from here.**

### Verified

| Gate check | Result |
|---|---|
| Screen renders real derived fields | 18 columns, roles with confidence, 2 pipeline notes — no placeholders |
| Edit `layman_name` + tier + `required_before_purchase`, submit | `status: approved`, `approved_by: merchant-console`, `version 2` |
| Edits intact after approval | `layman_name='Designer'`, `tier=1`, `required=True` |
| Reindex fired | `built: true`, 50 rows, `ollama:nomic-embed-text`, dim 768 |
| **Re-ingest preserves the edits** (`merge.py`) | v3: all four edits survived, `edited_fields=['Brand']` |
| **Required field blocks checkout** | "buy me one right now" → **no `preview` event**, tool withdrawn |
| **Answering it unblocks** | `known_slots['Brand']='Ray-Ban'`, `preview_transaction` back in the tool list |
| Static assets serve | 200 for html/css/config.js/review.js/app.js |
| JS syntax | `node --check` clean on all five files |
| DOM wiring | every `#id` referenced by the scripts exists in the HTML |
| Suites | agent **316** · payments **25** · merchant **47** |

### Decisions made mid-flight

1. **`web/config.js` is shared by both frontends, not per-console.** They run on one origin
   (`web/serve.py`), so one file means the two can never disagree about where a service
   lives. It also states plainly that the frontends never call payments directly — a browser
   that could reach `/payment/confirm` would route around the trust gate entirely.
2. **`web/serve.py` moved to :8090.** 8080 is already held by another process on this
   machine (PID 6212, not ours to stop).
3. **The console's catalog table reads `GET /catalog`, the normalized view — deliberately.**
   That endpoint exists for exactly this, and CONTRACTS §1.3 says the agent must never read
   it. The table is tolerant about field names so it cannot become a second place that
   decides what a product "is".
4. **The "product category" stat card now reports approval status**, not a hardcoded date. An
   unapproved profile is a guess, and a console that hides that is lying to the merchant.
5. **A 422 from the merchant's normalizer during upload is a warning, not a dead end** — the
   raw rows are stored regardless (phase 4b), so the agent can still use a sheet the console's
   own normalizer rejects.

### Left broken on purpose

- The **Agent activity** view is still entirely mock data. Out of scope for this plan; it
  reads as a dashboard, not as a claim about live behaviour.
- The eyewear catalog proposes **no cross-field rules**, so the rules table renders its empty
  state. The approve/edit/delete controls are written and wired but have not been exercised
  against a catalog that produces rules.
- The storefront still has no transport. Phase 7 — next.

### Next action

**Phase 7 — storefront SSE client.** Transport plus de-faking, per the rewritten phase 7 and
the "Storefront drop-in" section in the plan.

---

## Phase 7 — Storefront SSE client · ✅ complete

**Refs** `P` · closed 2026-08-29

### Files touched

**Rewritten:** `web/storefront/chatbot.js` — was 357 lines of simulation with no `fetch`
and no `EventSource`; now the real transport plus typed-event rendering.

**Edited:** `web/storefront/chatbot.html` · `web/storefront/chatbot.css`

**Deleted:** `web/storefront/products.csv` — the last hardcoded catalog.

### Transport

`POST /chat` consumed with `fetch` + a `ReadableStream` reader, **not `EventSource`**, which
cannot carry a request body — and both `/chat` and `/chat/confirm` need one. Frames are split
on the blank line, `event:`/`data:` parsed, `: keepalive` comments ignored, and a trailing
partial frame is kept in the buffer rather than dropped. One `session_id` is minted by the
first stream and reused for the rest of the conversation including the confirm stream.

Rendered from typed events only: `token` `tool_start` `products` `comparison` `probe`
`preview` `receipt` `error` `done` — **plus `cart` and `notice`**, the two the plan's original
phase 7 list omitted. `notice` matters: it carries the merchant-approved cross-field warnings
from phase 6, so without it a rule a merchant signed off in the console could never reach a
shopper. Nothing is scraped out of the token stream.

### De-faked

`products.csv`, `fallbackProducts`, `parseCsvLine`, `parseProductsCsv`, `loadProducts`,
`showComparisonResults`'s invented paragraph, and **both** definitions of `simulate()` are
gone — there were two, and the second silently won.

**The card-details step was removed**, per the decision recorded in the drop-in section.
Payments has no PAN field; `authorize` takes `{preview_id, method, proof}` and
`EXPLICIT_CONFIRM` *is* the consent. Collecting a card number we then discard is theatre that
teaches a judge the wrong thing about the architecture. The delivery-details step, the Visa
panel and the tokenized-card mock stay; **the consent checkbox is now the sole gate on the
confirm button**, which is the true design.

Currency is formatted with `Intl.NumberFormat` from the server's own `currency` field. The
hardcoded `€`/`EUR`, the `#NV-2042` receipt and the `€129.00` are gone. **No total is ever
recomputed client-side** — a client sum that disagrees with the ledger is the worst bug a
checkout can have.

### The anti-hardcoding rule, enforced

`.headphones` / `.buds` / `.watch` in the CSS were replaced by one neutral
`.product-image.placeholder`. Product cards render from `attributes[]`, each carrying the
column, the merchant-approved layman `label`, and a preformatted `display` string.

`grep -riE "headphone|earbud|watch|screen size|€|EUR|products\.csv"` over `web/storefront/`
returns only the comment explaining the removal.

### Verified — the real client file, against the real stack

Browsers cannot be driven from here, so `chatbot.js` was executed **as shipped** under a
minimal DOM shim in Node (`vm.runInContext`), talking to the live agent. Not a reimplementation
of the client — the actual file, running its actual transport and render paths. The shim
harvests its element ids from `chatbot.html` so it cannot drift from the real markup.

| Gate check | Result |
|---|---|
| Vague query → cards | tool chip · product cards · generic attributes · relaxed-filter caveat |
| Cart | line items, `subtotal=$180.06` from the `cart` event |
| Preview | `preview total=$180.06`, line items, **server expiry shown** |
| Confirm is the only path | confirm **disabled** until the consent box is ticked, then enabled |
| Receipt | `Order confirmed · 56b32765`, line items, `$180.06 charged on 8/29/2026, 8:38:36 PM` |
| **Second catalog, same code** | see below |
| Money in the ledger | 5 successful transactions, $1,120.88 total |
| DOM wiring | all 28 `#id` references exist in the HTML |
| `node --check` | clean on all frontend JS |
| Suites | agent **316** · merchant **47** · payments **25** |

**The category-agnostic claim, demonstrated rather than asserted.** `power_tools.csv` was
uploaded as a second merchant and the *identical* storefront code ran against it:

- The merchant's own normalizer **rejected** the file — `ok: false`,
  `missing_required_columns: ["price"]`, because the column is `price_usd`. **The raw rows
  were stored anyway** (26 rows, `id_column: sku`), exactly as phase 4b intended, so the agent
  could use a sheet the console could not.
- The agent derived `id=sku · title=product_name · price=price_usd · stock=qty_on_hand ·
  image=image_url` from column names it had never seen.
- The storefront rendered attribute labels **"power source · battery included · brand ·
  chuck size · tool type"** — every one of them from that merchant's columns — and completed
  a purchase through to a receipt for `$149.00`.

No file in `web/storefront/` was changed between the two runs.

### Decisions made mid-flight

1. **The "Checkout" button asks the agent for a total rather than computing one.** It sends
   "I'm ready to check out. Show me the total." so the preview comes from
   `preview_transaction` — which re-verifies price and stock against the merchant first
   (invariant 5). A button that built its own total would skip that check, which is the one
   that catches "it sold out while you were deciding".
2. **The suggestion chips are catalog-neutral** ("What do you have under 200?"). The shipped
   ones named headphones and earbuds, which is the same hardcoding as the CSS classes.
3. **A declined charge leaves the confirm button re-enabled** and the basket intact, matching
   the backend, which deliberately does not clear the cart on `PaymentError`.
4. **`Intl.NumberFormat` is wrapped in try/catch** — an unusual currency code from a merchant's
   sheet should degrade to "12.5 XYZ", not throw inside a render and blank the receipt.

### Left broken on purpose

- **`atlas-widget.js` is untouched.** It embeds `chatbot.html` in an iframe and needs no
  transport of its own.
- The **Agent activity** view in the console is still mock data (noted in phase 6).
- The delivery details collected are **not sent anywhere** — payments models no shipping
  address. The form is honest about being a demo step; wiring it would mean inventing a
  contract that does not exist.

### Next action

**Phase 8** — `docker compose up` for the whole stack, per-service images, and the root
`tests/smoke_test.py` that would have caught every finding in this plan on the day it was
introduced.

---

## Phase 8 — One command brings up the stack · ✅ complete

**Refs** `M` `Q` · closed 2026-08-29 · **Block B complete**

### Files touched

**New:** `tests/smoke_test.py` · root `pyproject.toml` · `.dockerignore` ·
`services/{agent,merchant,payments}/Dockerfile`

**Rewritten:** `docker-compose.yml` — Postgres plus all three services.

### The smoke test — the point of the whole plan

`tests/smoke_test.py`, 15 tests, against the three **real** services over HTTP.

Every per-service suite passes while the services disagree with each other, because each
tests against its own idea of the others. That is precisely how this repo ended up with an
agent that sent `id` where payments wanted `product_id`, read `total` from a response
carrying `amount`, and showed a 5-minute expiry the ledger enforced as 15. **Every finding in
the integration review would have been caught here on the day it was introduced.**

It is deliberately pointed at seams, not business logic. The three seams:

- **merchant → agent** — raw rows byte-identical to the uploaded file with column order
  preserved and every value still a string; `ids=` filtering server-side; roles derived from
  column names the agent has never seen; **a catalog the merchant's own normalizer rejects is
  still fully usable by the agent**, which is the normalization boundary made executable.
- **agent → payments** — `product_id`, currency not silently replaced, one expiry clock, the
  full preview → authorize → confirm → receipt path, `{code, message}` envelopes, and `?fail=`
  actually failing. Two assertions are negative — `"total" not in body` and
  `"timestamp" not in body` — because those are the fields the agent used to read, and a test
  that only checks the right fields exist would not notice them coming back.
- **agent → frontend** — SSE parsed the way the storefront parses it; `session` and `done`
  both present; every product attribute carrying `{column, label, display}` so the contract
  is generic *by construction*; confirmation only on its own endpoint.

It **skips cleanly** when the services are not up (verified by pointing it at a dead port:
15 skipped), so it can sit in CI before CI has a stack to run it against. It uses a
`smoke_<uuid>` merchant id so a run never mutates demo data.

### The stack

`docker compose up` brings up Postgres, merchant :8001, payments :8003, agent :8002.
One `Dockerfile` per service, uv-based, dependencies in their own layer so a code edit does
not reinstall the world.

- **The agent runs `--workers 1` and never `--reload`.** `SessionStore` is in-process; a
  second worker or a reload drops the shopper's cart mid-conversation. The comment in the
  compose file says so, because this is the kind of flag someone helpfully "improves".
- Inside the network, services reach `db:5432`, `merchant:8001`, `payments:8003` — the
  `environment:` blocks override the `localhost:5433` values `.env` carries for bare runs.
- **Ollama stays on the host**, reached via `host.docker.internal` with a `host-gateway`
  extra_host. Containerizing a 274 MB embedding model for a demo is not worth it.
- A named `agentdata` volume keeps derived profiles and vector indexes across rebuilds;
  without it every `compose up` re-embeds the whole catalog.
- The **frontends are not containerized**. They are six static files; `python web/serve.py`
  serves both from one origin. Adding nginx would be ceremony.

### Verified

| Gate check | Result |
|---|---|
| All three images build | `toes-merchant`, `toes-payments`, `toes-agent` Built |
| `docker compose config` | valid, 4 services |
| `docker compose up -d` | all four containers up |
| merchant health, in-container | `{"status":"ok","storage":"postgres","merchants":5}` |
| payments health, in-container | `{"status":"ok","db":"connected"}` |
| agent health, in-container | llm ✓ · embeddings ✓ · `merchant: http://merchant:8001` ✓ · `payment: http://payments:8003` ✓ |
| **Smoke test vs. the composed stack** | **15 passed** in 14.07s |
| Smoke test with the stack down | **15 skipped**, no errors |
| Full purchase through the composed stack | `Order confirmed · c2676abc`, `$149.00 charged` |
| Per-service suites | agent **316** · merchant **47** · payments **25** |

### Decisions made mid-flight

1. **The root `pyproject.toml` sets `[tool.uv] package = false`.** The repo root is a place to
   run the smoke test from, not a distributable package — without it setuptools auto-discovery
   trips over `web/`, `docker/` and `services/` as three competing top-level packages. Note
   `uv pip install -e` builds anyway and still warns; **`uv sync --all-extras` is the command
   that respects it.**
2. **`testpaths = ["tests"]` at the root deliberately does not collect the services.** They
   are three separate packages all named `app`, with their own venvs. Collecting them from one
   root process is exactly the import collision phase 1 spent effort removing.
3. **Dependency versions were not unified across services**, per the plan. They are separate
   processes with separate venvs; pinning them together would couple three release cadences
   for no benefit.
4. **The Dockerfiles tolerate a missing `uv.lock`** (`--frozen` with a fallback). All three
   have one today, and the fallback means adding a fourth service does not silently fail to
   build before its lock exists.

### Left broken on purpose

- **No CI workflow.** The smoke test is written to be CI-safe (it skips), but wiring an
  actual pipeline was not in this plan's scope.
- The **Agent activity** view in the console remains mock data.
- Merchant `requirements.txt` still sits beside `pyproject.toml`; nothing reads it. Removing
  it was phase 8's call and it is being kept — it is harmless, and the merchant is the one
  service someone might run without uv.
- `storage.search_catalog()` remains in the merchant though no route calls it.

### Next action

**None — the plan is complete.** Phases 1–8 are closed and every finding `A`–`Q` is fixed.
Before a demo: `docker compose up`, `python web/serve.py`, then
`uv run pytest tests/smoke_test.py -q` as the go/no-go. And do not restart the agent
mid-demo — its `SessionStore` is in-process.
