# Architecture

What each service is for, how a request flows through them, and which rules are structural
rather than stylistic.

For exact request and response shapes, read [CONTRACTS.md](CONTRACTS.md).
For where files live, read [../CLAUDE.md](../CLAUDE.md).

---

## The claim

A shopper who does not know the vocabulary of a product category can still buy the right
thing, by talking to an agent that learned the category from the merchant's own spreadsheet.

Two consequences follow, and everything else in this repo is downstream of them:

1. **No niche may be hardcoded.** The same code must shop power tools, tea, and eyewear. A
   column named `RRP inc VAT` and one named `price_usd` are both just "the price column" once
   the profiler has run. In the agent backend, a test that names a specific product attribute
   is a bug.
2. **Meaning is derived, then approved.** The pipeline proposes what each column means; the
   merchant signs it off. The agent has no domain authority the merchant did not grant.

---

## Four services, four roles

| Service | Port | Owns | Does not own |
|---|---|---|---|
| **Merchant backend** | 8001 | Catalog upload, raw-row storage, serving raw rows, merchant config | Meaning. Retrieval. Anything the shopper sees. |
| **Agent backend** | 8002 | Ingestion, the profile, retrieval, the agent loop, tools, session, the trust gate | Money movement. The merchant's own console views. |
| **Payments** | 8003 | Preview, consent, charge, the ledger | Anything conversational. It never sees a shopper. |
| **Web** | static | Merchant console, storefront | All business logic. It renders typed events and posts intents. |

The frontends hold **no logic that matters**. Everything they display arrives as a typed
event or an API response; everything they cause travels as an explicit POST.

---

## The flow

```
   any spreadsheet          ┌────────────────────────────────┐
   any category      ──────▶│ MERCHANT BACKEND        :8001  │
   any column layout        │ upload · store raw · serve raw │
                            └──────────────┬─────────────────┘
                                           │ raw rows, verbatim
                                           ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  AGENT BACKEND                                                     :8002  │
 │                                                                           │
 │  INGEST — runs once per catalog, identical code for every category        │
 │                                                                           │
 │   loader ─▶ coerce ─▶ profiler ─▶ canonicalize ─▶ classify ─▶ PROFILE     │
 │     │         │          │             │             │           │        │
 │   csv/xlsx  currency,  dtype,      cluster        LLM: tiers,  MERCHANT   │
 │   encoding, units,     null rate,  variant        layman copy, APPROVES   │
 │   header    list-cells cardinality spellings      cross-field      │      │
 │   sniffing                                        rules            ▼      │
 │                                                                           │
 │   rows ─▶ enrich (LLM descriptor) ─▶ embed ─▶ INDEX (.npy, per merchant)  │
 │                                                                           │
 │  RUNTIME — per shopper turn                                               │
 │   SSE /chat ─▶ agent loop ─▶ tools ─▶ typed events ─▶ storefront          │
 │                    │                                                      │
 │                policy gate: which tools are even visible this turn        │
 └───────────────────────────────────────────────────────────────────────────┘
                     │                                  │
     stock reverify  ▼                                  ▼  preview/authorize/confirm
      MERCHANT BACKEND :8001                        PAYMENTS :8003
                                                    ledger (Postgres)
```

### The conversational arc

```
  vague query          probe            narrow          decide         pay
  ──────────▶  ┌──────────────┐  ─────────▶  ┌───────────┐  ──▶  ┌─────────┐
  free-text    │ ask the 1-2  │  structured  │ compare   │      │ preview │
  intent, no   │ questions    │  filters     │ on the    │      │ confirm │
  known slots  │ that matter  │  over        │ axes THEY │      │ charge  │
       │       │ FOR THIS     │  candidates  │ care about│      │ receipt │
       ▼       │ CATALOG      │              └───────────┘      └─────────┘
  dense vector └──────────────┘
  retrieval          │
  (wide recall)   ALWAYS show results alongside the question.
                  Never a bare interrogation.
```

**Retrieve before you probe.** Probe-first reads as a form; retrieve-then-probe reads as a
salesperson. There must always be product cards on screen when the agent asks a question.

---

## The agent profile — where category knowledge is allowed to live

The central artifact, and the **only** place category knowledge may live. Generated at
ingest, approved by the merchant, then injected into the system prompt and used to drive
probing, filtering and comparison.

It carries: the derived `category`, the `roles` map (which column is id / title / price /
stock / image / text), a `fields` list giving each column a `kind`, `unit`, `tier`, a
`layman_name`, a `why_it_matters` and a `probe_question`, plus proposed `cross_field_rules`.

Two properties make it the hinge of the whole design:

- **It is versioned and merchant-approved.** `status` moves `draft → approved`. Cross-field
  rules default *unapproved* — they are the one part of the pipeline that cannot be validated
  against the data, which is why they need a human signature.
- **Merchant edits survive re-ingest.** `merge.py` preserves every field the merchant
  actually touched, tracked by `edited_fields`. Getting that list wrong silently discards
  their work on the next upload.

Full schema: [agent/CLAUDE.md](agent/CLAUDE.md).

---

## Trust, consent and transparency

These are enforced in `app/agent/policy.py`, **not requested in a prompt.** A prompt
instruction is not a gate.

1. **`confirm_and_pay` is absent from the model's tool list** until a valid confirmation
   token exists. Tool visibility is filtered per-turn. *Tool absence is the gate.*
2. **A charge requires a prior `preview_transaction`**, which mints a `preview_id`, a hash of
   the cart, and an expiry. It may be reached two ways — the model calling the tool
   mid-conversation, or `POST /chat/checkout`, which runs it directly with no model in the
   path. Both converge on the same `PreviewEvent`. A preview charges nothing, so which of
   them produced it is a reliability question, not a trust one; the gate is invariant 1.
3. **Confirmation arrives as a separate HTTP POST** (`/chat/confirm`), never inferred from
   chat text. No amount of typing "yes buy it" may fire a charge.
4. **Cart-change invalidation.** On confirm the cart hash is recomputed; if it differs from
   the previewed hash, the charge is rejected and a fresh preview is forced.
5. **The index is for discovery only.** Price and stock are re-verified against the merchant
   backend immediately before every preview. This makes `fetch_by_ids` the most
   safety-critical call in the agent. After a charge captures, the quantity sold is written
   back to the merchant's stored rows — best-effort, never blocking a receipt, since the
   money has already moved.
6. **Never recommend an out-of-stock item.** `stock > 0` is a default hard filter.
7. **No domain authority the merchant did not approve.** The agent explains what a field
   *means*; it never asserts fitness for a medical, safety or regulatory purpose. Cross-field
   rules apply only when present in the *approved* profile.
8. **Every preview and charge is logged** with timestamp, session, cart and outcome.

The same three-step shape appears on the payments side for the same reason:
`preview → authorize → confirm` are three endpoints because the shopper must see the exact
charge, explicitly consent to it, and only then have money move. **Collapsing them collapses
the consent design.**

---

## Structural constraints

**One uvicorn worker for the agent, no `--reload`.** `SessionStore` is in-process; carts live
in that process. A second worker or a mid-conversation reload loses live carts. This is a
demo-day failure mode, not a theoretical one.

**Embeddings need Ollama running**, or `EMBEDDING_PROVIDER=openai` as the one-line escape
hatch. A dead Ollama degrades `search_catalog` to structured-filter-only under a hard timeout
— it must never hang the SSE stream — but the demo is much weaker without vectors.

**Two clocks must not disagree.** The expiry the shopper is shown and the expiry the ledger
enforces are currently different numbers on different sides. Payments is authoritative,
because it is the side that refuses the charge. See CONTRACTS §2.7.

**Three top-level packages are named `app`** (agent, payments, and formerly the repo root).
Deleting the root one removed the hazard that actually bites; renaming the other two is
deferred until after the demo, as a recorded decision rather than an oversight.

---

## Where meaning is and is not derived

The one boundary worth restating, because violating it is silent:

> The merchant stores raw rows and serves them untouched. The agent interprets.

Normalizing at upload destroys the raw columns the profiler needs, and a normalized row
cannot be un-normalized. The merchant's `normalize` / `taxonomy` / `llm_client` are kept for
the merchant console's own product table and must never sit on the path that feeds the agent.

See CONTRACTS §0 for the full ownership split.
