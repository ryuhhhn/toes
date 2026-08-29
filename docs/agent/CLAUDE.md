# CLAUDE.md — Consumer / Agent Backend

## What this repo is

The **Consumer Backend** for a conversational commerce agent built for a Visa hackathon.
A shopper talks to an AI agent that helps them **discover → decide → pay** in one conversation.
This repo owns the agent: ingestion analysis, retrieval, orchestration, tools, session state,
and the trust gate that stands between conversation and a real charge.

### The core claim we are building toward

> Point this system at a spreadsheet of *any* product category, in *any* column layout, and
> it derives its own understanding of that category, asks the questions that matter for it,
> and sells from it — with no code change.

**This is the product.** It is validated by demonstrating **at least two unrelated niches
with two differently-shaped spreadsheets**, not by polishing one vertical. Treat single-niche
tuning as a bug.

### Hard rule: no niche may be hardcoded

No file under `app/` may reference a domain-specific column name, attribute, value, or piece
of category knowledge. Not in constants, not in prompts, not in fallbacks, not in tests.
Everything category-specific is **derived at ingest and stored in the Agent Profile**.

Illustrative examples throughout this document deliberately alternate between two
contrasting categories — **laptops** (numeric-spec-heavy) and **skincare**
(attribute/ingredient-heavy) — precisely so that no single one shapes the design. They are
examples, not targets.

If you catch yourself writing `if category == ...` or a per-niche YAML file, stop. The
mechanism is: **code enumerates → LLM interprets → merchant approves.**

---

## Team boundaries — what we own vs. what we consume

Four roles on this project. We are **Consumer Backend**.

| Role | Owns | We interact via |
|---|---|---|
| Frontend | Merchant site UI + consumer chat UI | They consume our SSE stream + typed events |
| Merchant Backend | File upload, storage, merchant config | We pull raw catalog rows from them |
| **Consumer Backend (us)** | Ingestion analysis, **retrieval/search**, agent loop, tools, session state | — |
| Payment/Auth | Mock Visa charge, user authorization, audit log | We call their preview/authorize/confirm |

### Scope amendments to the original team spec — authoritative

1. **Spec §2.3 "Retrieval Support" moved from Merchant Backend to us.**
   We own `search_catalog` end to end: indexing, embeddings, filtering, ranking.
   Their `GET /catalog/search` is **not used**; they should not build it.

2. **We own catalog *analysis*, they own catalog *storage*.**
   They accept the upload and store rows. We read rows back and derive the **Agent Profile**.
   Their spec §2.1 "LLM-assisted field mapping" is superseded by our ingestion pipeline —
   it should be deleted from their scope to avoid two systems guessing at the same thing.

3. **The spec's "category selector" dropdown is superseded.** Category config is *generated*
   per merchant at ingest and approved by the merchant. There is no fixed category list.

---

## Locked technical decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime | Python 3.13 + FastAPI, `uv` | Best LLM SDK support, native async SSE |
| Agent loop | **Hand-rolled.** No agent framework | Full control, debuggable live |
| LLM inference | **OpenAI primary, Anthropic toggle** via `LLM_PROVIDER` | Both behind one `LLMClient` protocol |
| Embeddings | **Ollama `bge-m3`**, OpenAI provider as config swap | Local, free, no cloud dependency |
| Vector store | **numpy matrix + cosine**, persisted `.npy` | Hundreds–thousands of rows per catalog. A vector DB is unjustified ops burden here |
| Transport | **SSE** (`text/event-stream`) | Confirmation travels as a separate POST by design |
| Session state | **In-memory dict behind a `SessionStore` protocol** | Zero infra; swap to Redis is one file |

### Non-negotiable operational constraint

`SessionStore` is in-process. **Run exactly one uvicorn worker**, and no `--reload` during a
demo. If carts vanish mid-conversation, this is why.

---

## Architecture

```
   any spreadsheet          ┌────────────────────────────────┐
   any category      ──────▶│ Merchant Backend (other team)  │
   any column layout        │ upload · store                 │
                            └──────────────┬─────────────────┘
                                           │ raw rows
                                           ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  CONSUMER BACKEND (this repo)                                             │
 │                                                                           │
 │  INGEST — runs once per catalog, identical code for every category        │
 │                                                                           │
 │   loader ─▶ coerce ─▶ profiler ─▶ canonicalize ─▶ classify ─▶ PROFILE     │
 │     │         │          │             │             │           │        │
 │   csv/xlsx  currency,  dtype,      cluster        LLM: tiers,  merchant   │
 │   encoding, units,     null rate,  variant        layman copy, APPROVES   │
 │   header    list-cells cardinality spellings      cross-field      │      │
 │   sniffing                                        rules            ▼      │
 │                                                                           │
 │   rows ─▶ enrich (LLM descriptor) ─▶ embed ─▶ INDEX (.npy, per merchant)  │
 │                                                                           │
 │  RUNTIME — per consumer turn                                              │
 │   SSE /chat ─▶ agent loop ─▶ tools ─▶ typed events ─▶ Frontend            │
 │                    │                                                      │
 │                policy gate: which tools are even visible this turn        │
 └───────────────────────────────────────────────────────────────────────────┘
                     │                                  │
     stock reverify  ▼                                  ▼  preview/authorize/confirm
            Merchant Backend                      Payment/Auth (other team)
```

### The conversational arc

```
  vague query          probe            narrow          decide         pay
  ──────────▶  ┌──────────────┐  ─────────▶  ┌───────────┐  ──▶  ┌────────┐
  free-text    │ ask the 1-2  │  structured  │ compare   │      │ preview │
  intent, no   │ questions    │  filters     │ on the    │      │ confirm │
  known slots  │ that matter  │  over        │ axes THEY │      │ charge  │
       │       │ FOR THIS     │  candidates  │ care about│      │ receipt │
       ▼       │ CATALOG      │              └───────────┘      └────────┘
  dense vector └──────────────┘
  retrieval          │
  (wide recall)   ALWAYS show results alongside the question.
                  Never a bare interrogation.
```

**Retrieve before you probe.** Probe-first reads as a form; retrieve-then-probe reads as a
salesperson. There must always be product cards on screen when we ask a question.

---

## Repo layout

```
app/
  main.py                  FastAPI app + router mounting
  config.py                pydantic-settings; ALL env access goes through here
  api/
    chat.py                POST /chat (SSE), POST /chat/confirm
    ingest.py              analyze / upload / get profile / approve profile
    catalog.py             index status, manual reindex
    session.py             GET /session/{id}  (debug/inspection)
    health.py
  agent/
    loop.py                Orchestration. "Which tool fires when" lives HERE
    prompt.py              System prompt assembled from the Agent Profile
    policy.py              Guardrails: tool visibility, probe budget, checkout gates
    events.py              SSE event schema (cross-team contract)
  llm/
    base.py                LLMClient protocol: stream_with_tools() + complete_json()
    openai_client.py       anthropic_client.py       factory.py
  embeddings/
    base.py                EmbeddingProvider protocol
    ollama_provider.py     openai_provider.py        factory.py
  ingestion/
    loader.py              FILE → DataFrame. csv/tsv/xlsx, encoding + delimiter + header sniffing
    coerce.py              Currency, unit-bearing numerics, list-cells, booleans, nulls
    profiler.py            DETERMINISTIC: dtype, cardinality, null rate, distributions
    canonicalize.py        Variant spelling clustering → canonical enums + alias map
    classify.py            LLM: category, tiers, layman copy, cross-field rule proposals
    schema_map.py          Columns → canonical roles (id/title/price/stock/image/text)
    bootstrap.py           Deterministic profile with no LLM — fallback + cold start
    merge.py               Preserve merchant edits across re-ingest
    profile_store.py       Versioned AgentProfile persistence
  retrieval/
    sync.py                Pull catalog snapshot from Merchant Backend
    enrich.py              LLM descriptor pass (cached by row hash)
    index.py               Build/persist/load numpy embedding matrix
    registry.py            Multi-merchant index registry (two live catalogs at once)
    filters.py             Predicate application + relaxation ladder
    search.py              filter-then-rank
  tools/
    registry.py            Single tool definition → emits OpenAI AND Anthropic schemas
    search_catalog.py  get_product_details.py  compare_products.py
    probe_attributes.py  build_cart.py  preview_transaction.py  confirm_and_pay.py
  session/
    models.py  store.py
  clients/
    merchant.py  payment.py
stubs/
  mock_services.py         Standalone merchant + payment stubs
fixtures/
  catalogs/                ≥2 unrelated categories, ≥2 file formats. See "Generality".
data/
  profiles/  index/        Generated artifacts. Gitignored.
```

---

## Core invariants — do not violate

These encode "Trust, Consent & Transparency" (20% of judging). Enforced in
`agent/policy.py`, not merely requested in a prompt.

1. **`confirm_and_pay` is absent from the model's tool list** until a valid confirmation
   token exists. Tool visibility is filtered per-turn by `policy.available_tools(session)`.
   A prompt instruction is not a gate; tool absence is.
2. **A charge requires a prior `preview_transaction`**, which mints a `preview_id` and a
   hash of the cart, with an expiry.
3. **Confirmation arrives as a separate HTTP POST** (`/chat/confirm`), never inferred from
   chat text. No amount of the user typing "yes buy it" may fire a charge.
4. **Cart-change invalidation.** On confirm, recompute the cart hash; if it differs from the
   previewed hash, reject and force a fresh preview.
5. **The index is for discovery only.** Price and stock are re-verified against the Merchant
   Backend immediately before preview.
6. **Never recommend an out-of-stock item.** `stock > 0` is a default hard filter unless the
   user explicitly asks otherwise.
7. **No domain authority the merchant did not approve.** The agent explains what a field
   *means*; it never asserts fitness for a medical, safety, or regulatory purpose.
   Cross-field rules are used only when present in the *approved* profile.
8. **Every preview and charge is logged** with timestamp, session, cart, and outcome.

---

## The Agent Profile

The central artifact, and the only place category knowledge is allowed to live. Generated at
ingest, **approved by the merchant**, then injected into the system prompt and used to drive
probing, filtering, and comparison.

```jsonc
{
  "merchant_id": "...", "version": 3, "status": "approved",   // draft | approved
  "category": "laptops",              // LLM-derived, never from a fixed list
  "source": { "filename": "stock_export.xlsx", "sheet": "Sheet1", "row_count": 412 },
  "roles": { "id": "sku", "title": "model_name", "price": "rrp_inc_vat",
             "stock": "qty_on_hand", "image": "img", "text": ["description","spec_notes"] },
  "fields": [
    {
      "column": "ram_gb", "kind": "numeric", "unit": "GB", "tier": 1,
      "bins": [[8,8],[16,16],[32,64]],
      "layman_name": "Memory",
      "why_it_matters": "Decides how many things you can run at once before it slows down.",
      "how_to_find_out": "If you keep 20+ browser tabs open, you want more.",
      "probe_question": "Roughly how heavy does your workload get?",
      "required_before_purchase": false
    },
    {
      "column": "skin_concern", "kind": "categorical_multi", "tier": 1,
      "canonical_values": ["dryness","acne","pigmentation","sensitivity"],
      "aliases": { "spots": "acne", "dry": "dryness", "dark spots": "pigmentation" },
      "layman_name": "What you're trying to improve",
      "required_before_purchase": true
    }
  ],
  "cross_field_rules": [
    { "if": "field_a > X AND field_b == Y", "then": "warn",
      "message": "<merchant-approved sentence>", "approved_by_merchant": true }
  ],
  "agent_tone": "Warm, concise, knowledgeable. Never pushy."
}
```

`tier` is the **prior**, blended at runtime with live information gain:

- **Tier 1** — blocking. Cannot sensibly choose without it.
- **Tier 2** — high regret if wrong.
- **Tier 3** — nice to have.

Only fields with `required_before_purchase: true` gate checkout, and that flag is set by the
**merchant** on the approval screen, never by the LLM.

**Validation rule:** every `canonical_values` entry the LLM returns must map back to values
that the *deterministic profiler actually found in the column*. Reject anything else. This is
the guard against the model inventing plausible attributes.

---

## Probe ranking

`probe_attributes` is **deterministic**. It decides *which* question is worth asking and
supplies the domain copy. **It does not phrase the question** — the LLM does, so the
conversation stays fluent while the selection stays testable.

For each field not in `known_slots ∪ asked_slots ∪ declined_slots`, over the **live candidate
set** `C`:

```
coverage(a) = fraction of C with a non-null value for a     # can't filter on missing data
H(a)        = normalized Shannon entropy of a's distribution in C          # 0..1
tier_w(a)   = {1: 1.0, 2: 0.6, 3: 0.3}[tier]

score(a)    = tier_w(a) × H(a) × coverage(a)
```

Skip rules: `distinct == 1` (no discriminating power), `distinct > 12` for categoricals,
`coverage < 0.5`. Numeric fields are quantile-binned (3–4 bins) before entropy.
Multi-valued categoricals are exploded before counting.

**Budgets:** `MAX_PROBES_PER_TURN=1`, `MAX_PROBES_PER_SESSION=4`. Past budget the tool is
withdrawn. An agent that interrogates loses.

---

## Ingestion pipeline

Every phase is category-agnostic. Phases 1–3 and 6 are pure code; only 4 uses an LLM.

| # | Phase | Module | LLM? |
|---|---|---|---|
| 1 | **Load** — csv/tsv/xlsx, encoding + delimiter sniffing, header-row detection, multi-sheet | `loader.py` | No |
| 2 | **Coerce** — currency, unit-bearing numerics, list-cells, booleans, null tokens | `coerce.py` | No |
| 3 | **Profile** — dtype, null rate, distinct count, cardinality ratio, samples, min/max | `profiler.py` | No |
| 4 | **Canonicalize** — fuzzy cluster variant spellings, then label | `canonicalize.py` | Labels only |
| 5 | **Classify** — category, tiers, layman copy, probe phrasing, proposed rules | `classify.py` | Yes |
| 6 | **Merchant approval** — draft → approved, with edit merge on re-ingest | `api/ingest.py`, `merge.py` | Human |
| 7 | **Enrich + index** — LLM descriptor per product, then embed | `retrieval/` | Yes |

### Phase 2 is where real spreadsheets are won or lost

Naive pipelines die on these. All of them are category-agnostic:

- **Currency** — `"$1,299.00"`, `"1.299,00 €"`, `"USD 45"`, `"45.00"` → `1299.00` + currency code.
  Detect thousands/decimal separator convention per column, not per cell.
- **Unit-bearing numerics** — `"52mm"`, `"16 GB"`, `"1.2kg"`, `"30 ml"` → value + unit.
  Store the unit on the field spec so the agent can speak it back correctly.
- **List-valued cells** — `"blue; red; green"`, `"wifi,bluetooth"`, `"vegan | cruelty-free"`.
  Sniff the delimiter, emit `categorical_multi`. Very common, and naive enum extraction
  silently produces one bogus enum value per unique *combination*.
- **Boolean-ish** — `yes/no`, `Y/N`, `true/false`, `1/0`, `✓`/blank.
- **Null tokens** — `""`, `"N/A"`, `"-"`, `"null"`, `"TBD"`, `"?"`.
- **Column names** — trim, collapse whitespace, dedupe collisions, handle unnamed columns.

**Rule: code enumerates, the LLM interprets.** Never ask the model to list a column's values;
it drops rare ones and invents plausible ones. Scan the column, then hand findings to the model.

Column-kind heuristic: all-unique → `identifier` · numeric (post-coercion) → `numeric` ·
2 distinct boolean-ish → `boolean` · URL regex → `url` · list-delimiter present →
`categorical_multi` · `distinct ≤ 25 and ratio < 0.3` → `categorical_enum` ·
mean length > 12 words → `free_text` · else ambiguous → LLM adjudicates.

### Required and optional columns

Required: an **id**, a **title/name**, a **price**. Without price there is no checkout.
Strongly recommended: **stock** (absent → assume in-stock and warn loudly on the approval
screen) and an **image url** (cards look broken without it).
Structural: headers in a detectable row, one product per row.

### Re-ingest must not destroy merchant edits

`merge.py`: when a catalog is re-uploaded, diff the new draft against the approved profile.
Fields the merchant edited keep their edits; genuinely new columns arrive as draft; removed
columns are marked stale, not silently dropped. Bump version, require re-approval only if
the field set changed materially.

---

## Search

**Filter first, then rank.** Hard constraints can never be ranked around.

1. Apply hard filters: `stock > 0`, price bounds, every `known_slot`.
2. If `len(candidates) < SEARCH_MIN_RESULTS`, walk the **relaxation ladder** — drop soft
   filters in reverse tier order (Tier 3 first) and record what was relaxed so the agent can
   say so out loud. A dead-end "no results" kills a conversation; a relaxed result with an
   honest caveat does not.
3. Embed the query; cosine similarity over the *filtered subset only*; top-k.
4. Return items, scores, `filters_applied`, `filters_relaxed`.

**Paraphrase → enum resolution:** for fields under ~50 canonical values, put the enum list in
the prompt and let the LLM map free text onto it — more accurate than cosine over two-word
strings, and free. Vector matching only for high-cardinality fields.

**Enrichment** is the biggest retrieval-quality lever: one LLM pass per product writing a
descriptor in *customer* language rather than catalog language, embedded alongside raw text.
The prompt for it must be written generically — it receives the derived category and field
list, and is never told what kind of product it is looking at ahead of time.

---

## SSE event contract (cross-team — coordinate before changing)

`POST /chat` returns `text/event-stream`. The frontend renders structured UI from **typed
events**, never by parsing prose out of the token stream.

| Event | Payload | Frontend renders |
|---|---|---|
| `token` | `{text}` | Streaming assistant text |
| `tool_start` | `{tool, summary}` | "Searching…" affordance |
| `products` | `{items: [ProductCard], filters_applied, filters_relaxed}` | Inline product cards |
| `comparison` | `{axes, rows}` | Side-by-side table |
| `probe` | `{attribute, question, why_it_matters, options}` | Question chips + explainer |
| `preview` | `{preview_id, items, subtotal, tax, total, expires_at}` | **Preview card + confirm button** |
| `receipt` | `{transaction_id, items, total, timestamp}` | Post-purchase summary |
| `error` | `{code, message}` | Inline error |
| `done` | `{turn_id}` | End of turn |

`ProductCard` fields are **derived from the profile's roles**, plus a `attributes` dict the
frontend renders generically. The frontend must not assume any category-specific attribute.

`POST /chat/confirm` — body `{session_id, preview_id}`. Not part of the stream. Returns a new
SSE stream carrying the `receipt` event.

---

## External contracts we consume

**Merchant Backend** (all we need — tell them the rest is unused):
- `GET /catalog?merchant_id=` → raw rows, any schema meeting the constraints above
- `GET /catalog?merchant_id=&ids=a,b,c` → stock/price reverification
- `POST {our_url}/ingest/analyze` → they call us after an upload (fallback: we poll on row hash)

**Payment/Auth**:
- `POST /payment/preview` · `POST /payment/authorize` · `POST /payment/confirm` ·
  `GET /payment/receipt/{transaction_id}`

Both stubbed in `stubs/mock_services.py`. **Build against the stubs first.**
`MERCHANT_BASE_URL` / `PAYMENT_BASE_URL` switch to real services with no code change.

---

## Configuration

| Var | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` \| `anthropic` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4.1` | |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-sonnet-5` | |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` \| `openai` |
| `OLLAMA_BASE_URL` / `OLLAMA_EMBED_MODEL` | `http://localhost:11434` / `bge-m3` | Fallback `nomic-embed-text` (768-dim, needs `search_query:` / `search_document:` prefixes) |
| `MERCHANT_BASE_URL` / `PAYMENT_BASE_URL` | stub | |
| `MAX_PROBES_PER_TURN` / `MAX_PROBES_PER_SESSION` | `1` / `4` | |
| `PREVIEW_TTL_SECONDS` | `300` | |
| `SEARCH_MIN_RESULTS` / `SEARCH_TOP_K` | `3` / `6` | |

**Ollama caveat:** embeddings need Ollama running locally. If deployed to a cloud URL for
judging, Ollama must be deployed alongside (extra container, ~1.2GB pull, CPU inference).
`EMBEDDING_PROVIDER=openai` is the one-line escape hatch. `search_catalog` applies a hard
timeout and degrades to structured-filter-only results — a dead Ollama must never hang the stream.

---

## Generality contract

Be precise about what actually generalizes:

- **Fully generic (pure code):** loading, coercion, profiling, type inference, enum
  extraction, canonicalization, embedding, retrieval, entropy probe ranking, comparison,
  cart, preview, confirm, receipt.
- **Generic via LLM + merchant approval:** category identification, field tiers, layman copy,
  probe phrasing, agent tone, enrichment descriptors.
- **Does NOT generalize on its own:** cross-field domain rules — field *interactions*, where
  an LLM will produce confident, plausible, unverifiable claims. Mitigation: the LLM
  *proposes*, the merchant approves/edits/deletes. The mechanism stays generic; correctness
  sits with the person who has the expertise.

### Enforcement

- `fixtures/catalogs/` holds **at least two unrelated categories in at least two file
  formats**, with deliberately different column-naming conventions (`snake_case` vs
  `Title Case With Spaces`), different currency formats, one with list-valued cells, one
  with unit-bearing numerics, both with messy variant spellings and nulls.
- `tests/test_generality.py` is **parameterized over every fixture** and asserts only
  category-agnostic properties: roles detected, no null-token leakage into enums, every
  canonical value traceable to real data, probe ranking returns a non-empty sensible order,
  search returns in-stock results, relaxation triggers rather than returning empty.
- **A test that names a specific product attribute is a bug.** If a test needs domain
  knowledge to assert, it belongs in a fixture-specific golden file, not the generic suite.

---

## Conventions

- Async throughout. One shared `httpx.AsyncClient` in app state.
- Pydantic models at every boundary — tool args, tool returns, SSE payloads.
- Tools are **pure functions of (args, session, profile)**. They never decide when they run;
  `agent/loop.py` does. That separation is the difference between a debuggable orchestrator
  and a pile of prompt-coupled side effects.
- Every LLM call goes through `llm/factory.py`. No direct SDK imports outside `app/llm/`.
- Every LLM output that will be persisted is **validated against deterministic findings**
  before it is stored.
- Log every tool call with args and latency. In a live demo the log is your only visibility.
- Tool schemas are defined **once** in `tools/registry.py`; adapters emit OpenAI's
  `{"type": "function", "function": {...}}` and Anthropic's `{"name", "input_schema"}`
  from the same source.
