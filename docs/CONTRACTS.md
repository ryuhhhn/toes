# Contracts

**The cross-team source of truth.** Every mismatch in this repo exists because three teams
each held a private idea of the same interface. This file is where the interface lives now.

Change a shape here **before** changing the code that implements it, and tell the other side.

Status key: ✅ agreed and implemented · 🚧 agreed, implementation pending · ❌ known mismatch,
fix scheduled.

Ports are fixed repo-wide: merchant `8001` · agent `8002` · payments `8003` · stubs `9001`.

---

## 0. The normalization boundary

**The single most important rule in this document.**

> **The merchant stores raw rows and serves them untouched. The agent derives all meaning.**

The agent's premise is that column layout is arbitrary: a catalog may name its price column
`RRP inc VAT` or `price_usd` or `Cost`, and the agent's profiler discovers which is which.
Normalizing at upload — coercing rows into a fixed nine-field `Product` — **destroys exactly
the information the profiler needs.** A normalized row cannot be un-normalized.

Therefore:

| Concern | Owner |
|---|---|
| Storing the uploaded sheet verbatim | merchant |
| Serving raw rows to the agent | merchant |
| Deriving roles (id/title/price/stock/image/text) | **agent** |
| Deriving category, tiers, layman copy, cross-field rules | **agent** |
| Retrieval, ranking, filtering | **agent** |
| Rendering the merchant's own product table | merchant |

The merchant's `normalize.py`, `taxonomy.py` and `llm_client.py` are **kept, for the merchant
console's own display only.** They must never sit on the path that feeds the agent. Their
category inference is superseded by the agent's ingestion pipeline, and
`GET /catalog/search` is retired — the agent owns retrieval and nothing else calls it.

This is a design conflict resolved, not a duplication to be tidied up later.

---

## 1. Merchant → Agent

Base URL: `MERCHANT_BASE_URL`, `http://localhost:8001` in the composed stack.

### 1.1 `GET /catalog/raw` 🚧

The agent's only catalog read. Two modes, one endpoint.

**Request**

| Param | Required | Meaning |
|---|---|---|
| `merchant_id` | yes | which catalog |
| `ids` | no | comma-separated row ids. **Absent means the whole catalog.** |

**Response — 200**

```json
{
  "merchant_id": "eyewear_co",
  "id_column": "sku",
  "row_count": 2,
  "columns": ["sku", "Product Name", "RRP inc VAT", "qty_on_hand"],
  "uploaded_at": "2026-08-30T09:14:02.881Z",
  "source_filename": "autumn_range.xlsx",
  "rows": [
    { "sku": "SKU1", "Product Name": "...", "RRP inc VAT": "£129.00", "qty_on_hand": "4" }
  ]
}
```

Rules the merchant must hold:

- **Raw columns, exactly as uploaded.** No coercion, no renaming, no normalization, no
  reordering. Column names keep their spaces, casing and punctuation.
- **Every value is a string or `null`.** Numbers are not parsed; `"£129.00"` stays
  `"£129.00"`. The agent's `coerce` step owns parsing, and it needs the original text to
  detect currency and units.
- **`row_count` is the count of rows returned**, not the size of the catalog.
- **`ids` filters.** Passing two ids returns exactly those two rows. This is finding `F`:
  today the param does not exist, the whole catalog comes back, and the agent's `fetch_by_ids`
  silently falls back to scanning row values.
- **`id_column`** is the merchant's storage-level id detection: the first column that is
  fully populated and fully unique. It is *not* a claim about semantics — deriving roles
  properly is the agent's job. Reference implementation: `_first_unique_column` in the
  agent's `stubs/mock_services.py`.
- Unknown `merchant_id` → **404**.
- **`columns`, `uploaded_at` and `source_filename`** describe the stored sheet, not the
  slice returned: they stay the same whether or not `ids` filtered. `columns` is the
  sheet's column order, which is the merchant console's only way to draw the merchant's
  own spreadsheet back to them without inventing a shape for it. The agent ignores all
  three. Added when the console stopped rendering a normalized nine-field table, which
  could not show a sheet whose required columns did not map.

**Consumer:** `app/clients/merchant.py` — `fetch_catalog()` (no `ids`) and `fetch_by_ids()`
(with `ids`). `fetch_by_ids` is the most safety-critical call in the agent, because
invariant 5 says price and stock are re-verified here immediately before every charge.

**Current state ❌:** the agent calls `GET /catalog`, which returns a **bare JSON list**, so
`fetch_catalog` raises `MerchantUnavailable` on every sync (finding `E`). Phase 4 adds
`/catalog/raw`; phase 4e repoints the client.

### 1.2 `GET /merchants` 🚧

```json
{ "merchants": [ { "merchant_id": "eyewear_co", "row_count": 50, "id_column": "sku" } ] }
```

**Consumer:** `MerchantClient.list_merchants()`. It swallows failures and returns `[]`, so a
missing endpoint degrades silently rather than erroring — which is why finding `G` went
unnoticed.

### 1.3 `GET /catalog` — the merchant's own view ✅

Normalized nine-field products. **Not an agent contract.** The agent must never read it.

**One rule it must now hold: it may never describe a sheet that is no longer stored.**
An upload whose required columns cannot be mapped returns 422 and normalizes to nothing.
The old code left the previous upload's products in place, so `/catalog` kept serving
*last week's sheet* while `/catalog/raw` served this week's — the console showed one
catalog and the agent sold from another. An upload now always resets this view: to the
normalized products when normalization succeeded, and to **empty** when it did not. An
empty table is a merchant asking "where did my products go?"; a stale one is a merchant
who never asks at all.

The console therefore draws its product table from `/catalog/raw`, which cannot go stale
and cannot fail to represent a sheet. `/catalog` remains for category and any consumer
that wants the coerced shape.

### 1.4 `POST /catalog/upload` ✅

`multipart/form-data`: `merchant_id` and `file`.

**Accepted formats:** `.csv`, `.tsv`, `.txt`, `.xlsx`, `.xls`. Excel is not a convenience
— merchants keep catalogs in spreadsheets, and a service that only reads CSV makes
"export to CSV first" a precondition for using the product at all. Parsing is by extension
with a CSV fallback, so a mislabelled file still gets a chance rather than a 400.

Two writes, in this order, and the order is the contract:

1. **Raw rows are stored first**, before normalization runs and regardless of what it
   does. The agent's whole premise depends on those columns surviving.
2. **The console's normalized view is reset**, always — see §1.3.

**Response — 200** `{report, products}` · **422** `{report, products: [], raw}` where
`raw` is `{merchant_id, id_column, row_count}` for the rows that *were* stored. A 422 is a
warning, not a dead end: the agent can sell from that sheet even when the console's
normalizer cannot draw it.

### 1.5 `POST /catalog/{merchant_id}/stock` ✅

Writes new values into stored raw rows. This is how the shop's inventory follows a sale.

```json
{ "updates": { "SKU1": { "qty_on_hand": "3" } } }
```

**Response — 200** `{ "updated": 1, "rows": [ ... ] }` · unknown merchant → **404**.

The **caller names the column**, and that is the whole point. The merchant does not know
which of its columns means stock — deriving roles is the agent's job (§0), and a merchant
that guessed would be normalizing by the back door. It writes the value it is handed into
the cell it is told, and nothing else. Values are stored as strings, like every other raw
value.

Unknown row ids are skipped rather than erroring, so a partially-stale cart still applies
the part that is real. `updated` is the count actually written.

**Consumer:** `MerchantClient.adjust_stock()`, called by `confirm_and_pay` **after** the
charge is captured. It is best-effort and never blocks a receipt: money has already moved,
and failing to write inventory is a bookkeeping problem, not a payment one. The endpoint
already existed in `stubs/mock_services.py` — this converges the real service onto the
shape the stub had, which is the direction §5 requires.

**On the index:** decrementing stock does not reindex. The index is a discovery snapshot
(invariant 5) and price and stock are re-verified against these very rows immediately
before every charge, so nothing unpurchasable can be sold. Search results may lag until
the next `POST /catalog/sync/{merchant_id}`.

### 1.4 `GET /health` 🚧

```json
{ "status": "ok", "storage": "postgres", "merchants": 3 }
```

`storage` is `"postgres"` or `"memory"`. It exists so nobody debugs a store they aren't
using — today `DATABASE_URL` is captured at import time, so setting it late silently yields
in-memory mode with no warning anywhere.

### 1.7 Upload notification

`POST {agent}/ingest/analyze` — the merchant calls the agent after an upload completes.
Fallback: the agent polls on a row hash. Either way the merchant does not wait on it.

### 1.8 CORS

The merchant app has **no CORS middleware today** and the console is served from another
origin. Phase 4c adds it. The agent already allows `*`.

---

## 2. Agent → Payments

Base URL: `PAYMENT_BASE_URL`, `http://localhost:8003` in the composed stack.

`preview → authorize → confirm` are **three endpoints on purpose.** The user sees the exact
charge, explicitly consents to it, and only then does money move. **Do not collapse them.**
That separation is the consent design, not an accident of layering.

### 2.1 `POST /payment/preview`

**Request**

```json
{
  "merchant_id": "eyewear_co",
  "session_id": "sess_...",
  "currency": "USD",
  "items": [
    { "product_id": "SKU1", "title": "...", "quantity": 1, "unit_price": 129.0 }
  ]
}
```

**Response — 200**

```json
{
  "preview_id": "...", "merchant_id": "...", "session_id": "...",
  "subtotal": 129.0, "total": 129.0, "currency": "USD",
  "items": [], "created_at": "...", "expires_at": "..."
}
```

✅ **Closed in phase 5.** What each mismatch was, and what it is now:

| | Was | Now |
|---|---|---|
| item key | agent sent `id` | both sides use **`product_id`** |
| `currency` | dropped by `Cart`, response hardcoded `"USD"` | `Cart.currency` (default `"USD"`), echoed through |
| `expires_at` | not returned | **returned**, derived from `created_at` + `PAYMENT_TTL_MINUTES` |
| `tax` | read but never returned | still not returned — payments has no tax in scope, so the agent reads `0.0`. By design. |

`expires_at` is a pydantic computed field over `created_at`, not a stored column, so it
cannot drift from the TTL that `ledger_service.is_expired` enforces. One clock.

**On `tax`:** payments deliberately has no tax, so `total == subtotal`. The agent's
`PreviewEvent` carries a `tax` field that will simply be `0.0` against real payments and
non-zero against the stub. The **shopper-facing total must come from the server**, never be
recomputed client-side.

### 2.2 `POST /payment/authorize`

**Request**

```json
{ "preview_id": "...", "method": "explicit_confirm", "proof": true }
```

`method` is one of `mock_otp` · `mock_biometric` · `explicit_confirm`. The agent's flow is
`explicit_confirm` — the enum member already exists in payments and is exactly this case.

**Response — 200**

```json
{ "authorized": true, "authorization_id": "...", "method": "explicit_confirm", "preview_id": "..." }
```

✅ **Closed in phase 5.** The agent now sends exactly
`{preview_id, method: "explicit_confirm", proof: true}`. `session_id`/`user_id` are gone from
the body: the preview already carries them, and the authorization is bound to the preview.

Authorization failure → **401**.

### 2.3 `POST /payment/confirm`

**Request**

```json
{ "preview_id": "...", "authorization_id": "..." }
```

✅ This one always worked. The agent's extra `session_id` was dropped in phase 5 for
honesty, not to fix a break.

**Response — 200** is a `Transaction`:

```json
{
  "transaction_id": "...", "preview_id": "...", "amount": 129.0, "currency": "USD",
  "status": "success", "failure_reason": null, "created_at": "..."
}
```

✅ **Closed in phase 5.** `confirm_and_pay` used to read `total`, `timestamp` and `items`
from this response, none of which exist — hence a receipt event showing `0.0`, an empty item
list and a blank timestamp. It now reads **`amount`** and **`created_at`**, and builds the
line items from `session.active_preview.items`.

**The rule this establishes:** a receipt's line items come from the preview the shopper saw
and authorised, never from what the payment service echoes back. The session is the
authoritative record of what was agreed to; the ledger is authoritative for the money (§4).

**Refusal codes**, each an audited `confirm_blocked` event before the refusal is raised:

| Status | Reason | Meaning |
|---|---|---|
| 404 | `preview_not_found` | cannot charge for something never shown |
| 410 | `preview_expired` | consent has a shelf life |
| 401 | `authorization_invalid` | missing, or bound to a different preview |
| 401 | `authorization_not_approved` | consent was not granted |
| 410 | `authorization_expired` | |
| 402 | *(charge declined)* | `detail` is the failure reason |

**Idempotency:** confirming an already-charged preview returns the existing `Transaction`,
not an error. A retry after a lost response converges on the truth and never charges twice;
`UNIQUE(preview_id)` enforces it at the database level. Callers must treat a repeated
`transaction_id` as success.

### 2.4 `GET /payment/receipt/{transaction_id}`

**Response** is a `ReceiptView`, a three-part envelope:

```json
{
  "transaction": {},
  "authorization": {},
  "events": [ { "event_type": "...", "reason": null, "created_at": "..." } ]
}
```

✅ **Closed in phase 5.** `receipt()` now unwraps to the transaction's fields and keeps
`authorization` and `events` reachable under their own keys.

A failed charge persists nothing, so its receipt lookup **404s**: no receipt exists because
no money moved.

### 2.5 Error envelope ✅

**Every** payments error is `detail: {code, message}`. Real payments used to send a plain
string, so against it every failure collapsed to the generic `payment_failed` and the agent
could not tell a declined card from an expired preview — two situations that need different
sentences.

Codes: `preview_not_found` · `preview_expired` · `authorization_invalid` ·
`authorization_not_approved` · `authorization_expired` · `authorization_failed` ·
`card_declined` · `unknown_transaction`, plus any injected code from §2.6.

The confirm-path codes are **the same strings the ledger records** as the `reason` on its
`confirm_blocked` events, so the audit trail and the agent never disagree about what went
wrong.

### 2.6 Failure injection `?fail=<code>` ✅

Supported on all three endpoints by **both** the stub and real payments, returning 402 with
`{code, message}`. Codes: `insufficient_funds` · `card_declined` · `network_error` ·
`expired_preview`. It previously existed only on the stub, and since FastAPI ignores unknown
query params it was a **silent no-op** against the real service — a scripted "declined card"
simply succeeded.

Two details that matter:

- **On `confirm` the failure is injected into the charge itself**, not at the router door, so
  a declined charge writes `charge_attempted` then `charge_failed` exactly as a real one
  does. A decline the ledger has no record of would defeat the purpose of the ledger.
- **It reaches a conversation through `POST /chat/confirm`'s optional `fail` field**, which
  the agent stores on the session and passes at capture time. It is deliberately *not*
  applied to authorize: authorize records consent rather than checking a card, and a failure
  there surfaces as a bare HTTP 402 the agent never gets to speak about. Injected at capture,
  the decline arrives as a typed `error` event and the agent offers an alternative with the
  cart left intact.

It can only ever turn a success into a failure — never skip a gate.

### 2.7 Two expiry clocks ✅ (finding `I`)

| Side | Setting | Value |
|---|---|---|
| agent | `PREVIEW_TTL_SECONDS` | 300 (5 min) |
| payments | `PAYMENT_TTL_MINUTES` | 15 |

The shopper was shown one deadline and the ledger enforced another. **Payments is
authoritative** — it is the side that refuses the charge.

✅ **Closed in phase 5.** The preview carries `expires_at` and the agent displays the
server's value. `PREVIEW_TTL_SECONDS` is demoted to a fallback used only when the payment
service sends no expiry. The confirmation token now expires *with its preview* rather than
on a third clock of its own.

---

## 3. Agent → Frontend: the SSE event contract

Transport: `POST /chat`, `text/event-stream`. Each event is
`event: <type>` then `data: <json>`, terminated by a blank line.
Source of truth: `app/agent/events.py`.

> **Render from typed events only.** Never parse prose out of the token stream. A frontend
> that scrapes structure out of `token` text is broken by the next prompt edit.

| Event | Payload |
|---|---|
| `token` | `{text}` — prose, for display only |
| `tool_start` | `{tool, summary}` |
| `products` | `{items[ProductCard], filters_applied[], filters_relaxed[], total_candidates, note}` |
| `comparison` | `{axes[], rows[]}` |
| `probe` | `{attribute, question, why_it_matters, how_to_find_out, options[]}` |
| `cart` | `{items[], subtotal, currency}` |
| `preview` | `{preview_id, items[], subtotal, tax, total, currency, expires_at}` |
| `receipt` | `{transaction_id, items[], subtotal, tax, total, currency, timestamp}` |
| `notice` | `{level, message, columns[]}` — a **merchant-approved** cross-field warning, never model-authored prose |
| `error` | `{code, message}` |
| `done` | `{turn_id}` |

### `ProductCard` — deliberately generic

```json
{
  "id": "...", "title": "...", "price": 129.0, "currency": "USD",
  "image": "...", "description": "...", "in_stock": true, "stock": 4.0,
  "score": 0.82,
  "attributes": [
    { "column": "ram_gb", "label": "Memory", "value": 16, "unit": "GB", "display": "16 GB" }
  ]
}
```

Roles come from the profile; everything else lands in `attributes`, which the frontend
renders **without knowing what any of it means**. `label` is the merchant-approved layman
name and is what the shopper sees — never `column`.

> **No category-specific field name may appear in frontend rendering code.** A storefront
> that hardcodes "screen size" breaks the core claim as surely as a backend that does. This
> is the same rule the agent backend holds itself to: a test naming a specific product
> attribute is a bug.

### Checkout is its own POST — `POST /chat/checkout` ✅

`{session_id}` → an SSE stream carrying `tool_start`, then `preview`, then `done`.

**Why this exists.** The checkout button used to send the chat message *"I'm ready to
check out. Show me the total."* and wait for the model to decide to call
`preview_transaction`. That made a button press a request for a favour. The panel stayed
shut whenever the model answered in prose instead, and — worse — it also stayed shut for
every *legitimate* refusal, because `policy.available_tools` withdraws the preview tool
when the basket is empty or a `required_before_purchase` field is unsettled. The shopper
saw the same nothing either way.

A preview is not a creative act. It is a deterministic function of the cart: re-verify,
price, mint, show. So this endpoint runs `preview_transaction` **directly, with no model
in the path**, and it either produces a preview or says exactly why it cannot.

**Refusals**, all before the stream opens, all `detail: {code, message}`:

| Status | Code | Meaning |
|---|---|---|
| 404 | `unknown_session` | no session; the shopper has not talked to the agent yet |
| 409 | `empty_cart` | nothing to check out |
| 409 | `checkout_blocked` | `policy.can_checkout` said no — message is its reason, verbatim |
| 503 | `catalog_unavailable` | no index for this merchant |

A tool failure *after* the stream opens (reverification, payments) arrives as a typed
`error` event, exactly as it does on `/chat`.

**This does not weaken the trust gate; it strengthens the symmetry.** Preview and charge
are now both button-driven endpoints rather than one button and one hope. The gate has
never been about who *asks* for a preview — a preview charges nothing. It is about
`confirm_and_pay` being absent from the model's schema until `/chat/confirm` mints a
token, and that is untouched. The model may still call `preview_transaction` itself
mid-conversation; both paths converge on the same `PreviewEvent`.

### Confirmation is its own POST

`POST /chat/confirm` with `{session_id, preview_id}` returns a second SSE stream carrying the
`receipt`. **A charge is never inferred from chat text** — no amount of the shopper typing
"yes buy it" may fire one. The trust gate is enforced by tool absence:
`confirm_and_pay` is filtered out of the model's tool list until a confirmation token exists,
and tokens are minted only by this endpoint.

### `filters_relaxed`

When present, the UI must say so. An honest "I widened the search" is the difference between
a relaxed result and an apparent wrong answer.

---

## 4. Audit authority (finding `L`)

Two logs, two different questions, one join key.

| Log | Authoritative for | Location |
|---|---|---|
| Payments ledger (Postgres) | **Money.** What was charged, consented to, refused, when. | `payments` db — `preview_created`, `auth_granted`, `charge_attempted`, `charge_succeeded`, `charge_failed`, `confirm_blocked` |
| Agent `audit.jsonl` | **What the agent did and why.** Tool calls, previews shown, reasoning. | `data/audit.jsonl` |

**They join on `preview_id`.**

Neither is a substitute for the other. If they disagree about money, **the ledger wins** — it
is the side with a database constraint forbidding a double charge. If they disagree about what
the shopper was shown, the agent log wins. A charge with no corresponding agent audit entry is
an incident, not a discrepancy.

---

## 5. Change protocol

1. Edit this file first.
2. Say which side changes and in which phase.
3. Update `stubs/mock_services.py` to match — **the stub is a contract implementation, not a
   convenience.** Every one of the mismatches above exists because the stub drifted from the
   real service and the agent was built against the stub.
4. The root `tests/smoke_test.py` (phase 8) runs the real seam end to end. It is the only
   thing that would have caught these on the day they were introduced.
