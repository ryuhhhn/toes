# Build Plan — Consumer / Agent Backend

Read `CLAUDE.md` first for architecture and invariants. This document is the execution order
and the design detail behind each module.

**Organising principle:** stages follow *dependency*, not deadline triage. Each stage is
independently testable and leaves the system in a working state. Stage I (generality
validation) is not optional polish — it is the proof of the core product claim, and it is
written to fail loudly if any niche-specific assumption creeps in.

Two ideas govern every stage:

> **Code enumerates, the LLM interprets, the merchant approves.**
> **Nothing category-specific may live outside the Agent Profile.**

---

## Stage A — Foundations

Unblocks everything, including the other two teams.

### A1. Project skeleton
- [x] `pyproject.toml`, `.env.example`, `.gitignore`
- [x] `app/config.py` — pydantic-settings. **No `os.environ` anywhere else.**
- [ ] `app/main.py` — app factory, shared `httpx.AsyncClient` in lifespan, router mounting
- [ ] `app/api/health.py` — reports provider reachability (LLM key present, Ollama up,
      merchant/payment stubs reachable). This one endpoint saves an hour of demo-day panic.

### A2. LLM provider abstraction — `app/llm/`

The single most important interface in the repo. Both SDKs normalise to one event stream.

```python
# base.py
@dataclass class TextDelta:  text: str
@dataclass class ToolCall:   id: str; name: str; arguments: dict
@dataclass class StopReason: reason: str            # "end_turn" | "tool_use" | "length"
LLMEvent = TextDelta | ToolCall | StopReason

class LLMClient(Protocol):
    async def stream_with_tools(
        self, *, system: str, messages: list[Msg], tools: list[dict]
    ) -> AsyncIterator[LLMEvent]: ...

    async def complete_json(
        self, *, system: str, user: str, schema_hint: dict, max_retries: int = 1
    ) -> dict: ...
```

Internal neutral message format (each client converts on the way out):
`UserMsg(content)` · `AssistantMsg(content, tool_calls)` · `ToolResultMsg(tool_call_id, name, content)`

Implementation notes:
- **OpenAI streaming**: accumulate `delta.tool_calls` by `index`; arguments arrive as
  fragmented JSON strings and must be concatenated before parsing.
- **Anthropic streaming**: `content_block_start` (`type == "tool_use"`) then
  `input_json_delta.partial_json` fragments; same accumulate-then-parse.
- `complete_json` is non-streaming. Parse leniently (strip ```json fences), and on failure
  retry **once** with the parse error appended to the prompt. Used by canonicalize, classify,
  enrich, and paraphrase resolution.

- [ ] `base.py` · `openai_client.py` · `anthropic_client.py` · `factory.py`

### A3. Embedding provider abstraction — `app/embeddings/`

```python
class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], *, kind: Literal["query","document"]) -> np.ndarray
    @property def dim(self) -> int
```

- [ ] Ollama impl — `POST /api/embed`, batched (`EMBED_BATCH_SIZE`), **hard timeout**
- [ ] OpenAI impl — `text-embedding-3-small`
- [ ] The `kind` parameter exists because some models need asymmetric prefixes
      (`nomic-embed-text` wants `search_query:` / `search_document:`). `bge-m3` does not.
      Encoding this in the interface stops a silent quality regression on a model swap.
- [ ] L2-normalise on the way out so similarity is a plain dot product

### A4. Downstream stubs — `stubs/mock_services.py`

Standalone FastAPI on `:9001` implementing the full Merchant + Payment contracts, so this
repo is never blocked by another team.

- [ ] Merchant: `GET /catalog`, `GET /catalog?ids=`, serves whichever fixture is loaded
- [ ] Payment: `/payment/preview`, `/payment/authorize`, `/payment/confirm`,
      `/payment/receipt/{id}` — with configurable failure injection (`?fail=insufficient_funds`)
      so the failure path can be demoed, not just the happy path
- [ ] A `stock` mutation endpoint so "it went out of stock while you were deciding" is
      demonstrable

**Acceptance for Stage A:** `/health` is green; a script streams a tool call from *both*
providers through the same interface; a script embeds two strings and prints cosine similarity.

**Send to the other teams now:** the SSE event table, the Agent Profile shape, and the fact
that `GET /catalog/search` and their §2.1 field-mapping are cancelled.

---

## Stage B — Ingestion I: file → normalized frame

Category-agnostic, pure code, heavily unit-tested. This is where real spreadsheets are won.

### B1. `ingestion/loader.py`

```python
def load_table(source: bytes | Path, *, filename: str, sheet: str | None = None) -> LoadedTable
# LoadedTable: df, filename, sheet, detected_encoding, detected_delimiter, header_row, notes[]
```

- [ ] Format dispatch by extension **and** content sniffing: `.csv`, `.tsv`, `.txt`,
      `.xlsx`, `.xls`
- [ ] Encoding detection — try `utf-8`, `utf-8-sig` (Excel's BOM is extremely common),
      `cp1252`, `latin-1` in order
- [ ] Delimiter sniffing via `csv.Sniffer` with a comma fallback
- [ ] **Header-row detection** — real exports have title/logo rows above the header.
      Scan the first ~10 rows, pick the first where cells are mostly non-null, mostly
      unique, and mostly non-numeric.
- [ ] Multi-sheet `.xlsx`: if one sheet, take it; if several, pick the one with the most
      data rows and record the others in `notes` for the merchant to override
- [ ] Column-name hygiene: trim, collapse internal whitespace, drop fully-empty columns,
      dedupe collisions (`price`, `price_2`), name unnamed columns (`column_4`)
- [ ] Drop fully-empty rows; record counts in `notes`

### B2. `ingestion/coerce.py`

Each coercer returns `(series, CoercionReport)` and is applied only when it wins a confidence
check across the whole column — **never cell-by-cell guessing**.

- [ ] **Null tokens** → `NaN`: `""`, `"N/A"`, `"NA"`, `"-"`, `"--"`, `"null"`, `"none"`,
      `"TBD"`, `"?"`, `"n/a"`. Case-insensitive. Run first; everything downstream depends on it.
- [ ] **Currency** → float + ISO code. Strip symbols and codes; **detect the
      thousands/decimal convention per column** (`1,299.00` vs `1.299,00`) by testing both
      against the whole column and taking the one that yields fewer failures. Per-cell
      guessing produces silent 1000× price errors.
- [ ] **Unit-bearing numerics** → value + unit: `"52mm"`, `"16 GB"`, `"1.2 kg"`, `"30ml"`,
      `"6.1-inch"`. Require ≥80% of non-null cells to share one unit before coercing.
      Store the unit on the field spec so the agent speaks it back correctly.
- [ ] **List cells** → `list[str]`: sniff `;` `|` `,` `/` by consistency across the column.
      Guard: a `,` inside free text is not a list delimiter — require a low mean token count
      per element and a small union of distinct elements relative to row count.
- [ ] **Booleans**: `yes/no`, `y/n`, `true/false`, `1/0`, `✓`/blank, `in stock`/`out of stock`
- [ ] **Percentages**: `"15%"` → `0.15`
- [ ] Every coercion emits a report so the approval screen can show *"we read `Price` as
      currency (USD), 3 cells unparseable"*. Transparency here is cheap and builds merchant trust.

**Acceptance:** unit tests per coercer, including the adversarial cases above. A fixture with
European-format prices must not produce 1000× values.

---

## Stage C — Ingestion II: deterministic understanding

No LLM. Produces enough structure that the system can already search and probe.

### C1. `ingestion/profiler.py`

```python
@dataclass class ColumnProfile:
    name, raw_dtype, kind, null_rate, distinct_count, cardinality_ratio,
    samples: list, value_counts: dict, numeric_min, numeric_max,
    unit: str | None, currency: str | None, mean_token_len: float, coercion: CoercionReport
```

- [ ] Column-kind heuristic exactly as specified in CLAUDE.md
- [ ] `value_counts` capped (top 50) — it feeds both canonicalization and probe entropy
- [ ] Flag columns that are ≥95% null as `unusable` and exclude them from probing

### C2. `ingestion/schema_map.py`

Role detection: `id`, `title`, `price`, `stock`, `image`, `text[]`.

- [ ] Synonym tables per role, matched exact → normalized → fuzzy (`rapidfuzz`, threshold ~85)
- [ ] **Corroborate names with data shape**, because a column called `code` may not be the id:
      - `id`: all-unique, non-null
      - `price`: numeric post-coercion, positive, currency coercion succeeded
      - `stock`: numeric non-negative integer, or boolean
      - `image`: URL-shaped
      - `text`: longest mean token length
- [ ] Return confidence per role; anything below threshold surfaces on the approval screen
      as an explicit question rather than a silent guess
- [ ] Config override so a merchant can correct a mapping

### C3. `ingestion/bootstrap.py`

```python
def bootstrap_profile(profiles: list[ColumnProfile], roles: Roles, merchant_id: str) -> AgentProfile
```

A complete, valid Agent Profile derived with **zero LLM calls** — roles detected, kinds set,
enums extracted, every field tier 2, layman copy absent.

Three jobs, which is why it is worth building carefully:
1. Unblocks Stages E–H before the LLM pipeline exists
2. Permanent runtime fallback when a merchant has no approved profile
3. Degradation path when the LLM is unavailable at ingest time

**Acceptance:** the system can search and probe a brand-new catalog using only Stages B–C.

---

## Stage D — Ingestion III: derived understanding + approval

### D1. `ingestion/canonicalize.py`

- [ ] Deterministic pre-pass: normalize (casefold, strip, collapse punctuation/whitespace),
      group exact matches, then fuzzy-cluster with `rapidfuzz.token_set_ratio ≥ 85`
- [ ] Representative = highest-frequency member of each cluster
- [ ] LLM pass **labels clusters only** — it receives the clusters and returns a display label
      per cluster. It never sees a request to "list the possible values."
- [ ] **Validation:** every returned label must be traceable to a real cluster. Reject and
      fall back to the representative otherwise.
- [ ] Output `{column: {canonical_values: [...], aliases: {raw → canonical}}}`
- [ ] For `categorical_multi`, cluster the *exploded* elements, never the combinations

### D2. `ingestion/classify.py`

One structured LLM call. Input: column profiles + canonical enums + ~10 sample titles +
role mapping. **Never the raw table** — profiles only, which keeps the prompt small and
prevents the model from inventing rows.

Output schema:
```jsonc
{ "category": "...", "category_confidence": 0.0,
  "fields": [ { "column", "tier", "layman_name", "why_it_matters",
                "how_to_find_out", "probe_question", "suggested_required_before_purchase" } ],
  "cross_field_rules": [ { "if", "then", "message" } ],
  "agent_tone": "..." }
```

- [ ] Prompt is written **category-blind** — it is told to derive the category, never given one
- [ ] Validation before persisting: every `column` must exist; `tier ∈ {1,2,3}`; no field may
      be invented; `suggested_required_before_purchase` is a *suggestion* the merchant must
      confirm; cross-field rules referencing unknown columns are dropped
- [ ] `approved_by_merchant` defaults to **false** on every proposed rule

### D3. `ingestion/profile_store.py` + `merge.py`

- [ ] Versioned JSON under `data/profiles/{merchant_id}/v{n}.json` + a `current` pointer
- [ ] `merge_profiles(approved_old, draft_new)` — the re-ingest problem:
      - fields the merchant edited keep their edits (track `edited_fields` on the profile)
      - unchanged fields take the new draft's values
      - new columns arrive as draft
      - vanished columns are marked `stale: true`, not deleted
      - re-approval is required only if the field set changed materially
- [ ] Audit: who approved, when, which version

### D4. `api/ingest.py`

- [ ] `POST /ingest/analyze` — `{merchant_id}`; pulls rows from Merchant Backend, runs B→D,
      saves **draft**, returns it
- [ ] `POST /ingest/analyze/upload` — accepts a raw file directly (multipart). Dev path *and*
      the live "onboard an unrelated catalog" demo. Supports the same formats as the loader.
- [ ] `GET /ingest/profile/{merchant_id}` — draft or approved, plus coercion reports and
      low-confidence role warnings, for the approval screen
- [ ] `PUT /ingest/profile/{merchant_id}` — merchant's edited version → `status=approved`,
      records `edited_fields`, bumps version, triggers reindex
- [ ] `GET /ingest/report/{merchant_id}` — human-readable ingestion summary (what we read,
      what we could not parse, what we guessed). High-trust, low-cost.

**Coordination:** the approval screen is Frontend work driven by this JSON. Send them the
shape as soon as D1 starts — it is the last cross-team dependency.

**Acceptance:** two unrelated fixture catalogs each produce a sensible profile, with correct
categories, plausible tiers, and zero invented fields.

---

## Stage E — Retrieval

### E1. `retrieval/sync.py`
- [ ] Pull snapshot, hash rows (stable hash over sorted id→row), store under
      `data/index/{merchant_id}/`. Skip reindex when the hash is unchanged.

### E2. `retrieval/enrich.py`
- [ ] Batch ~20 products per LLM call; generate a **customer-language descriptor** per product
- [ ] The prompt receives the derived category and field list and is otherwise category-blind
- [ ] Cache by `(row_hash, profile_version)`; only new/changed rows re-run
- [ ] Fail-open: enrichment failure degrades to raw text, never blocks indexing

### E3. `retrieval/index.py` + `registry.py`
- [ ] Embed `title + text fields + canonical attrs + descriptor`; persist `.npy` + `meta.json`
- [ ] Store `embedding_model` and `dim` in meta; **rebuild automatically on model change**
      (a silent dimension mismatch is a nasty failure)
- [ ] `registry.py` — multiple merchant indices resident at once. Required for the two-niche
      demo: switching catalogs must not mean restarting the server.

### E4. `retrieval/filters.py`
- [ ] Predicate types: equality, one-of, range, boolean, multi-valued contains
- [ ] `stock > 0` applied by default
- [ ] **Relaxation ladder** — while `len(results) < SEARCH_MIN_RESULTS`, drop the
      lowest-priority soft filter (Tier 3 → 2 → 1), recording each drop. Hard filters
      (stock, explicit price ceiling) are never relaxed.

### E5. `retrieval/search.py`
```python
async def search(merchant_id, query, filters, k) -> SearchResult
# SearchResult: items[], scores[], filters_applied, filters_relaxed, total_candidates
```
- [ ] Filter → embed query → cosine over the filtered subset only → top-k
- [ ] Embedding timeout → degrade to filter-only ordering (by price, then stock), flagged
- [ ] `total_candidates` is returned because the probe tool needs the live candidate set

**Acceptance:** parameterized over both fixtures — a vague query returns plausible in-stock
items; an over-constrained query relaxes rather than returning empty; an out-of-stock item
never appears.

---

## Stage F — Agent core

### F1. `session/`
```python
Session: id, merchant_id, messages[], cart, known_slots, asked_slots, declined_slots,
         probe_count, last_candidate_ids[], active_preview, confirmation_token, created/updated
```
- [ ] `SessionStore` protocol + `InMemorySessionStore` with TTL sweep
- [ ] `last_candidate_ids` is what makes probing work on the *live* set rather than the
      whole catalog — populate it on every search

### F2. `agent/events.py`
- [ ] Pydantic model per SSE event from the CLAUDE.md table
- [ ] `ProductCard` built from profile roles + a generic `attributes` dict

### F3. `tools/registry.py`
```python
@dataclass class ToolDef:
    name: str; description: str; parameters: dict; handler: Callable
    def to_openai(self) -> dict
    def to_anthropic(self) -> dict

async def handler(args: dict, ctx: ToolContext) -> ToolResult
# ToolResult: llm_content (what the model sees) + events (what the frontend renders)
```
The split in `ToolResult` is the key design decision: one tool call feeds both the model's
reasoning and the UI, without the model having to describe the UI in prose.

### F4. `agent/prompt.py`
- [ ] Assemble the system prompt from the Agent Profile: category, tone, field list with
      layman names, approved cross-field rules, and the behavioural rules (retrieve before
      probing, never claim unapproved domain authority, never imply a purchase is complete)
- [ ] The prompt is a **template filled from the profile** — no category words are literals

### F5. `agent/loop.py`
- [ ] Multi-round loop: stream LLM → emit `token` deltas → on tool call, emit `tool_start`,
      execute, emit the tool's events, append the result, re-enter. Cap at `MAX_TOOL_ROUNDS`.
- [ ] Tool list per round comes from `policy.available_tools(session)` — never a static list
- [ ] Errors inside a tool become an `error` event plus a tool-result message so the model
      can recover conversationally instead of the stream dying

### F6. `api/chat.py`
- [ ] `POST /chat` → `StreamingResponse` of `text/event-stream`
- [ ] Heartbeat comment every ~15s so proxies do not close idle streams
- [ ] Client disconnect cancels cleanly without corrupting session state

---

## Stage G — Discovery tools

- [ ] **`search_catalog(query, filters)`** — merges `known_slots` into filters, calls
      `retrieval.search`, stores `last_candidate_ids`, emits `products`
- [ ] **`get_product_details(id)`** — full record, rendered generically from the profile
- [ ] **`compare_products(ids[])`** — structured `{axes, rows}`, never prose.
      **Axis selection**: `known_slots` first, then Tier 1→3, capped at ~6 axes. Showing what
      *this shopper* said they care about instead of a generic spec dump is a small change
      that reads as intelligence.
- [ ] **`probe_attributes()`** — the scoring formula from CLAUDE.md, over `last_candidate_ids`
      - quantile-binning for numerics; explode `categorical_multi` before counting
      - skip rules: `distinct == 1`, `distinct > 12` categorical, `coverage < 0.5`
      - never re-ask: respects `asked_slots` and `declined_slots`
      - "don't care" resolves to an answered wildcard
      - returns ranked attributes with copy; **does not phrase the question**
- [ ] **Paraphrase → enum resolution** in `search_catalog`: free-text answers mapped onto
      canonical values via the in-prompt enum list, writing results into `known_slots`
- [ ] Policy rule enforced in the loop: **never emit `probe` without `products` in the same
      turn**

**Acceptance:** on both fixtures, a vague opening query returns cards *and* asks a Tier 1
question; answering visibly narrows the set; no question repeats; probing stops at budget.

---

## Stage H — Commerce tools + the trust gate

The rubric-critical path. Build the tests alongside; they *are* the deliverable here.

- [ ] `tools/build_cart.py` — add/remove/update quantity
- [ ] `clients/payment.py` — preview / authorize / confirm / receipt
- [ ] `tools/preview_transaction.py`:
      1. re-verify price and stock via `merchant.fetch_by_ids` — **abort on mismatch**
      2. `POST /payment/preview`
      3. mint `preview_id` + `cart_hash`, store with expiry
      4. emit `preview`
- [ ] `agent/policy.py` — `available_tools(session)`. `confirm_and_pay` is **omitted from the
      schema list entirely** unless a valid confirmation token exists.
- [ ] `POST /chat/confirm` — validates preview exists, unexpired, `hash(cart)` unchanged;
      calls `POST /payment/authorize`; mints a single-use confirmation token; re-enters the
      loop with `confirm_and_pay` now visible
- [ ] `tools/confirm_and_pay.py` — `POST /payment/confirm`, emit `receipt`, burn the token
- [ ] Append-only JSONL audit of every preview and charge

**Tests — these are the rubric, write them as assertions:**
- [ ] `confirm_and_pay` is absent from the emitted tool schema without a token
- [ ] "yes buy it now" typed in chat charges nothing
- [ ] cart changed after preview → confirm rejected
- [ ] expired preview → rejected
- [ ] confirmation token cannot be replayed
- [ ] item went out of stock between preview and confirm → clean abort with a useful message
- [ ] payment failure (`?fail=insufficient_funds`) surfaces conversationally, cart intact

---

## Stage I — Generality validation

**This stage is the product claim.** It is not optional.

### I1. Fixture corpus — `fixtures/catalogs/`

At least two catalogs that are unlike each other on every axis that matters:

| Axis | Catalog A | Catalog B |
|---|---|---|
| Category | numeric-spec-heavy | attribute/ingredient-heavy |
| Format | `.csv`, UTF-8 BOM | `.xlsx`, multi-sheet |
| Column naming | `snake_case` | `Title Case With Spaces` |
| Currency | `$1,299.00` | `1.299,00 €` |
| Special shape | unit-bearing numerics (`16 GB`, `6.1-inch`) | list-valued cells (`vegan; cruelty-free`) |
| Mess | variant spellings, null tokens | junk rows above the header, an unnamed column |

Both should include out-of-stock rows and at least one unparseable cell.

### I2. `tests/test_generality.py` — parameterized over every fixture

Asserts only category-agnostic properties:
- [ ] Roles detected with acceptable confidence; required roles present
- [ ] No null token ever appears as a canonical enum value
- [ ] Every canonical value traces to data the profiler actually saw
- [ ] No LLM-invented columns survive validation
- [ ] Currency coercion produces values in a sane order of magnitude
- [ ] Probe ranking returns a non-empty, correctly-ordered list, and never suggests a
      single-valued or mostly-null column
- [ ] Search returns only in-stock items; over-constrained queries relax
- [ ] The full purchase path completes end to end

> **A test that names a specific product attribute is a bug.** Domain-specific assertions
> belong in per-fixture golden files, never in the generic suite.

### I3. Anti-hardcoding guard
- [ ] A test that greps `app/` for fixture-specific vocabulary and fails if any appears.
      Crude, but it is the only thing that reliably stops niche leakage under pressure.

### I4. Cold-start rehearsal
- [ ] A script that wipes `data/`, ingests a catalog the system has never seen, approves the
      profile unmodified, and runs a scripted conversation to purchase. This is the demo,
      run as a test.

---

## Stage J — Hardening & observability

- [ ] Ollama timeout → filter-only degradation, never a hung stream
- [ ] LLM rate-limit / error handling inside SSE: emit `error`, keep the stream alive
- [ ] Session TTL sweep
- [ ] `GET /session/{id}` — full session inspection: slots, candidates, cart, tool history
- [ ] **`GET /trace/{session_id}`** — per-turn tool calls with args, latency, token counts.
      This is what lets you debug a bad answer live instead of guessing.
- [ ] Structured logging of every tool call
- [ ] Startup validation: index dim matches the configured embedding model; profile schema
      version matches the code

---

## Stage K — Extensions, in value order

1. **Runtime cross-field rule evaluation** — fire approved warnings during search and
   comparison. The visible expert-knowledge moment, and the strongest remaining item.
2. **Probe quality eval harness** — for a set of scripted openings, measure how much the
   candidate set actually shrinks per question asked. Turns "the probing feels smart" into a
   number you can improve and quote.
3. Product families / variant grouping (same product, different size or colour)
4. Redis `SessionStore`
5. Merchant-facing analytics (queries received, probes asked, conversions)
6. Voice input

---

## Cross-team messages to send early

| To | Message | Blocking for them from |
|---|---|---|
| Merchant Backend | `GET /catalog/search` cancelled; §2.1 LLM field mapping cancelled; we need raw rows + `ids=` filter + a post-upload webhook | Stage A |
| Frontend | SSE event table; `ProductCard` is generic (`attributes` dict, no fixed keys) | Stage F |
| Frontend | Agent Profile shape — they own the merchant approval screen, which is not in their original spec | Stage D |
| Payment/Auth | We call preview/authorize/confirm/receipt; we need a failure-injection mode | Stage H |

---

## The demo this plan is built to support

1. Merchant uploads a messy spreadsheet in Category A → the system reports what it read, what
   it could not parse, and what it guessed; proposes field tiers and layman copy; merchant
   approves
2. Shopper opens with a vague, non-technical request → product cards appear immediately
3. The agent asks the one question that actually matters *for this catalog*, explains **why**
   in plain language, with cards still on screen
4. Candidates narrow; side-by-side comparison on the axes the shopper named
5. Transaction preview — unmissable, distinct from chat
6. Explicit button press → user authorization → mock Visa charge → receipt
7. **Then: upload a completely unrelated spreadsheet in Category B, in a different file
   format, and run the same flow with no code change.** The system derives a different
   category, different tiers, and asks entirely different questions.

Step 7 is the point. Everything else is table stakes.
