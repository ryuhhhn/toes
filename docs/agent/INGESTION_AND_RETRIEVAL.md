# Ingestion & Retrieval — technical reference

Scope: everything between "merchant uploads a spreadsheet" and "shopper's search returns
ranked, filtered products." Companion to `CLAUDE.md` (architecture/invariants) and
`PROGRESS.md` (build status) — this doc goes one level deeper into how each module actually
works, for anyone changing this code rather than presenting it.

```
spreadsheet
    │
    ▼
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌──────────────┐   ┌──────────┐   ┌───────────┐
│  load   │──▶│ coerce  │──▶│ profile │──▶│ canonicalize │──▶│ classify │──▶│ approval  │
│ (code)  │   │ (code)  │   │ (code)  │   │ (code+LLM)   │   │  (LLM)   │   │ (human)   │
└─────────┘   └─────────┘   └─────────┘   └──────────────┘   └──────────┘   └───────────┘
                                                                                   │
                                                            approved AgentProfile  ▼
                                                        ┌──────────┐   ┌─────────┐   ┌───────┐
                                                        │  enrich  │──▶│  embed  │──▶│ index │
                                                        │  (LLM)   │   │ (code)  │   │ (.npy)│
                                                        └──────────┘   └─────────┘   └───────┘
                                                                                          │
                                                        shopper query ────────────────────▼
                                                                              ┌──────────────────┐
                                                                              │ filter, then rank │
                                                                              └──────────────────┘
```

Every phase is category-agnostic code except **canonicalize** (labelling only) and
**classify** (the one real interpretive call). Everything the LLM returns in both is
validated against what the deterministic phases actually measured before it can reach a
stored profile.

---

## 1. Load — `app/ingestion/loader.py`

`load_table()` turns arbitrary bytes into a clean, string-typed `DataFrame`. Values are kept
as strings deliberately — type inference is a separate, reportable phase (coerce), so pandas
guessing here would hide decisions the merchant needs visibility into.

- **Format detection is content-first.** `detect_format()` checks magic bytes (`PK\x03\x04` →
  xlsx/zip, `\xd0\xcf\x11\xe0` → legacy xls) before the file extension, because a
  misnamed upload is common.
- **Text decoding** tries `utf-8-sig → utf-8 → cp1252 → latin-1` in order and records which
  one won (`decoded_encoding`).
- **Delimiter sniffing** (`sniff_delimiter`) runs `csv.Sniffer` but only trusts its answer if
  that delimiter also appears repeatedly in the raw character counts — the Sniffer is
  confidently wrong on prose-heavy files.
- **Header row detection** (`detect_header_row`) scores each of the first 12 rows on
  `density × uniqueness × textual-ness` (`_header_score`) and picks the highest. Real
  exports routinely have banner/contact rows above the actual header; a perfect early match
  (`score ≥ 0.99`) short-circuits the scan.
- **Excel** is read with `dtype=object, keep_default_na=False` — not `dtype=str`, which would
  turn empty cells into the literal string `"nan"`. A `_stringify()` pass afterwards handles
  the float→int case so an integer SKU doesn't arrive as `"1001.0"`.
- Multi-sheet workbooks: the sheet with the most non-empty rows is chosen automatically
  unless `sheet=` is passed explicitly.
- Column names are trimmed, whitespace-collapsed, unnamed columns become `column_N`, and
  collisions are deduped (`clean_column_names`).
- Every decision above is appended to `LoadedTable.notes` as a `LoadNote(level, message)` —
  this is what the merchant approval screen surfaces as "we read your file this way."

## 2. Coerce — `app/ingestion/coerce.py`

Coercion is decided **per whole column, never per cell** — the confidence check that gates
each coercer is what stops a single stray value from producing a silently wrong column-wide
interpretation (e.g. a 1000x price error from one cell that happens to parse differently).

Fixed order, first match wins (`COERCERS` tuple):

```
boolean → currency → percentage → unit_numeric → plain_numeric → list
```

Order matters: boolean before numeric (a `1/0` flag must not become a measurement), currency
and unit before plain numeric (their markers must not be stripped blindly first), list last
(a delimiter inside otherwise-numeric text must not win).

- **Nulls** are decided once, up front (`normalise_nulls`), against one explicit token list
  (`NULL_TOKENS`: `""`, `-`, `n/a`, `tbd`, `null`, …). `"none"` is deliberately **not** in
  that list — it's frequently a real enum level (a rating, an included-extras flag), and
  destroying a real value is a correctness bug while leaking a rare null token into an enum
  is merely cosmetic.
- **Currency** (`try_currency`) requires ≥60% of non-null cells to carry a recognizable
  symbol or ISO code, then detects US vs. EU decimal convention **for the whole column** by
  voting on separator position across every value (`detect_decimal_convention`) —
  `1,299.00` vs `1.299,00` is resolved once, not cell-by-cell.
- **Unit-bearing numerics** (`try_unit_numeric`) — `"18 V"`, `"1.2kg"`, `"13mm"` — require
  ≥80% of cells to match `_NUMBER_UNIT`, and the dominant unit must cover ≥80% of matches or
  the column is left as text rather than silently averaging incompatible units.
- **List-valued cells** (`try_list`) sniffs a delimiter. Unambiguous delimiters (`;`, `|`)
  need only 25% of rows to contain them and short elements; ambiguous ones (`,`, `/`) need
  60% coverage, short elements, and a small distinct-element set — because prose is full of
  commas. URLs are explicitly excluded first (`URL_RE` check) so an image-URL column is never
  shredded on `/`.
- Every coercer returns a `CoercionReport(applied, detail, failed_cells, total_cells, …)`
  regardless of outcome, so the approval screen can say e.g. *"read Retail Price as currency
  (EUR, European decimal convention), 1 cell unparseable."*

## 3. Profile — `app/ingestion/profiler.py`

Zero LLM. Pure statistics per column: `null_rate`, `distinct_count`, `cardinality_ratio`,
value counts (capped at 50), samples (capped at 5), numeric min/max. This is the
**deterministic source of truth** — the classify phase later is checked against it, and
anything the LLM proposes that doesn't trace back to a value recorded here is rejected.

- Multi-valued cells are **exploded** before counting (`_explode`) — counting combinations
  instead of elements is exactly how naive enum extraction invents one bogus value per
  unique combination.
- `_decide_kind()` implements the column-kind heuristic from `CLAUDE.md`, in priority order:
  `unusable` (null rate ≥ 95%) → coercion result (`boolean`/`categorical_multi`/`numeric`) →
  `url` → `free_text` (mean token length > 12, or > 5 tokens at > 90% uniqueness) →
  `identifier` (> 95% unique, < 5% null, ≤ 2 tokens, and `_looks_like_code` — digit-heavy or
  uniform-length) → `categorical_enum` (≤ 25 distinct **and** either cardinality ratio < 0.3
  or distinct ≤ 12) → else `categorical_high_card`.
- The `distinct ≤ 12` branch of the enum test exists specifically for short catalogs: on a
  100-row catalog the cardinality ratio is inflated by row count alone, and without this
  branch every genuine enum — everything worth asking the shopper about — would be
  misclassified as high-cardinality text.
- `_looks_like_code()` is what stops `identifier` from swallowing description and image-URL
  columns just because they're all-unique — it additionally demands the values look
  machine-generated (≥70% contain a digit, or tightly uniform length).

## 4. Role mapping — `app/ingestion/schema_map.py`

Assigns `id / title / price / stock / image / text[]` structural roles. This runs
immediately after profiling, in the same no-LLM phase, and is exempt from the
anti-hardcoding rule — "retail price" and "units in stock" name universal commerce roles,
not category knowledge (see `tests/test_anti_hardcoding.py`).

- Each `(role, column)` pair gets a score = `0.55 × name_score + 0.45 × shape_score`
  (`_combined`) — a name match alone is not trusted (a column called `"code"` is often not
  the id; `"cost"` is sometimes supplier cost, not retail price), and shape alone is not
  trusted either.
- `shape_score` is role-specific: `price` wants a successful currency coercion (1.0) or at
  least a non-negative numeric column (0.55, "plausible but unproven"); `stock` wants a
  non-negative integer numeric or boolean; `image` wants `ColumnKind.URL`; `id` wants
  `ColumnKind.IDENTIFIER`.
- Roles are claimed **greedily, most structurally distinctive first**
  (`ROLE_ORDER = id, price, stock, image, title, text`), and a column can only be claimed
  once. Below `ACCEPT_THRESHOLD = 0.45` a role is left unset rather than guessed, and
  recorded with a reason (`low_confidence_roles()` surfaces these to the approval screen).
- `text` is multi-valued (up to `MAX_TEXT_COLUMNS = 3`), everything else is single-column.
- Merchant overrides (re-ingest, manual correction) always win outright and are stamped
  `confidence = 1.0, reason = "set by the merchant"`.

## 5. Canonicalize — `app/ingestion/canonicalize.py`

Groups variant spellings of the same value into one canonical form, for
`categorical_enum` / `categorical_multi` / `categorical_high_card` columns only.

- **Deterministic first** (`cluster_values`): exact match after normalization (casefold,
  strip punctuation, collapse whitespace) groups obvious duplicates; then a fuzzy pass
  (`fuzz.ratio` on normalized forms, threshold **88**) folds rarer variants into commoner
  ones, gated by a rarity ratio (`RARITY_RATIO = 0.34`) — a variant only merges into a
  cluster that is meaningfully more common than itself, so two similarly-common near-spellings
  are treated as a real distinction, not a typo.
- This is a **deliberate deviation from `BUILD_PLAN.md`**, which specified
  `rapidfuzz.token_set_ratio ≥ 85`. `token_set_ratio` scores 100 for any label that contains
  another as a token subset, so a value and its own qualified variant (e.g. two genuinely
  different enum values, one a superset phrase of the other) would silently fuse and delete
  a real distinction.
- **LLM labelling is optional and cosmetic** (`label_clusters`): given clusters that are
  already grouped, the model is asked only to pick or re-case the best display spelling —
  never to invent, translate, or expand an abbreviation, never to merge or add clusters. A
  returned label is accepted **only if it re-normalizes back to the exact cluster it was
  given** (`normalise_value(label) == cluster.key`); anything else is rejected and logged.
  This is what makes it structurally impossible for this step to introduce a value the data
  never contained.

## 6. Classify — `app/ingestion/classify.py`

The one real interpretive LLM call: category, per-field tiers, layman copy, probe phrasing,
proposed cross-field rules.

- **Category-blind by construction.** The prompt is told to *derive* what's being sold from
  column profiles, never handed a category to confirm — confirming a stated premise is a
  known way to get a classifier to agree with a wrong one.
- **Sees measurements, never rows.** `_column_digest()` sends kind, null rate, distinct
  count, numeric range/unit/currency, or up to 15 canonical values / 3 samples — never the
  underlying data. This keeps the prompt small and, more importantly, means the model has
  no rows to generalize from and no way to report a value the profiler never measured.
- **`validate_classification()` is the actual guard**, not the prompt wording:
  - a field entry referencing an unknown column, or a duplicate column, is dropped and
    logged in `rejected[]`
  - `tier` outside `{1, 2, 3}` is coerced to 2 and logged
  - a cross-field rule is kept only if its `if` expression textually references at least one
    known column name (`_referenced_columns`); otherwise dropped
  - every proposed rule is stamped `approved_by_merchant=False` — the model can never
    self-approve its own claim
- `apply_classification()` enforces the deterministic/derived split explicitly: it only ever
  writes `tier, layman_name, why_it_matters, how_to_find_out, probe_question,
  suggested_required_before_purchase` onto a `FieldSpec`. `canonical_values`, numeric
  ranges, unit, currency, null rate, coercion, kind are measured earlier and are not
  writable from this function — enforced by construction, not convention.
- `suggested_required_before_purchase` is exactly that — a suggestion. The real
  `required_before_purchase` flag that gates checkout is merchant-set only (`policy.py`).
- Any failure (LLM down, bad JSON) leaves the deterministic profile from phases 1–4
  untouched and appends a warning note; ingestion never fails on this step.

## 7. Enrich — `app/retrieval/enrich.py`

One short LLM-written sentence per product, in *customer* language, generated **after**
profile approval and used only to improve embedding retrieval quality (see §9 below for the
honest limits of this).

- Prompt is category-blind at call time too — it receives the approved category and field
  list, never told in advance what kind of product it's describing (`_describe_batch`).
- Batched 20 products per call (`BATCH_SIZE`), each product reduced to `{name, description,
  <field label>: <value + unit>, …}` via `_product_payload()` — the same display names and
  units the shopper-facing UI uses, not raw column names.
- Rules baked into the system prompt: one sentence, ≤ 30 words, use only given facts, no
  invented features/materials/certifications, no marketing superlatives, never a medical,
  safety, or regulatory claim.
- **Cached** by `sha256(profile_version + row JSON)` per product (`descriptors.json` next to
  the index), so re-indexing after a catalog update only pays for rows that actually
  changed.
- A returned descriptor for a product id that wasn't in the batch sent is discarded — that
  would be a hallucinated row, not a real product.
- Fail-open throughout: no LLM configured, or a batch call fails → indexing proceeds on raw
  catalog text with no descriptors, never blocks the ingest.

## 8. Embed & index — `app/retrieval/index.py`, `app/embeddings/`

**Document construction** (`build_document`) — one embeddable string per product:
`title. text-role columns. "{field display name}: {value} {unit}" for every active field.
descriptor.` The attribute-phrase step (`_attribute_phrases`) is what makes a bare number
like `18` meaningful in embedding space at all — it's rendered from the *profile's* display
name and unit, which is why this stays category-agnostic; nothing here names a specific
attribute.

**Embedding provider** — `EmbeddingProvider.embed(texts, kind="query"|"document")`
(`app/embeddings/base.py`, `EmbedKind` protocol). Default: Ollama running
`nomic-embed-text` (768-dim) via `langchain_ollama`; `EMBEDDING_PROVIDER=openai` swaps
providers with no other code change.

- **Asymmetric prefixing** (`apply_prefix`): `nomic-embed-text` wants `"search_document: "`
  prepended for indexed text and `"search_query: "` for live search text — silently omitting
  this measurably hurts retrieval. LangChain sends whatever string it's given, so the prefix
  is applied by the app *before* the text reaches the client, keyed off model-name prefix
  (also covers `mxbai-embed-large`, `e5-*`).
- **L2 normalization** (`l2_normalise`) happens once, at the provider boundary, so every
  later cosine-similarity computation is a plain dot product.
- **Timeout + degrade, never hang.** `embed()` wraps the network call in
  `asyncio.wait_for`; a timeout or connection failure raises `EmbeddingUnavailable` rather
  than propagating — callers are required to catch this and degrade (see §9).
- **A fresh client per call**, deliberately (`_make_client`). The provider itself is
  `lru_cache`d, so a long-lived `OllamaEmbeddings` instance would bind its async connection
  pool to whichever event loop constructed it and silently fail on every other one — this
  was found running the code, not designed for up front, and surfaced as a vector-free index
  rather than an error.

**Storage** — no vector database. `vectors.npy` (numpy matrix) + `rows.json` (ids + raw
rows) + `meta.json` (embedding model id, dimension, row hash, profile version, build
timestamp) per merchant, under `data/index/<merchant_id>/`.

- `load_index()` rejects and forces a rebuild if the stored `embedding_model` doesn't match
  the currently configured one, or if the vector count doesn't match the row count — a
  silent dimension mismatch doesn't error, it just returns nonsense scores, so this check
  exists specifically to fail loud instead.
- Building without a reachable embedder still produces a valid `CatalogIndex`
  (`matrix=None`) — filtering, price-ranking, and selling all still work; only paraphrase
  matching is lost. `meta["vectors"] = False` and `meta["error"]` record why.

## 9. Search — `app/retrieval/search.py`, `app/retrieval/filters.py`

**Filter first, then rank — always, never the reverse.** Ranking the whole catalog by
similarity and filtering afterwards would both be slower and *wrong*: it can truncate good
in-scope matches out of the top-k before the filter is even applied.

1. **Hard filters always applied:** `sellable_predicate` (has a parseable price — a product
   that can't be bought must not be recommended into a dead end) and, unless explicitly
   overridden, `in_stock_predicate` (boolean `True`, or numeric `≥ 1`).
2. **Slot filters** (`predicates_from_slots`) — every `known_slot` becomes a `Predicate`
   typed by the field's own `kind`: numeric slots become `RANGE` or `EQ`, booleans go
   through `parse_boolean` (never Python's `bool()` — `bool("No") is True`), multi-values
   become `IN`, everything else `EQ`. A slot naming an unknown column is dropped, not
   guessed at — the guard against a hallucinated filter reaching the catalog.
3. **Relaxation ladder** (`filter_with_relaxation`) — if the survivor count is below
   `SEARCH_MIN_RESULTS`, soft (non-`hard`) predicates are dropped **Tier 3 first**, one at a
   time, re-filtering after each drop, until enough remain or nothing droppable is left.
   `hard=True` predicates (out-of-stock, an explicit price ceiling the shopper stated) are
   never relaxed — relaxing those would silently answer a different question than the one
   asked. Every drop is recorded in `FilterOutcome.relaxed` so the agent can say out loud
   what it let go of.
4. **Ranking:** the query is embedded with `kind="query"` (`embed_query`, same
   timeout-and-degrade contract as indexing) and cosine-scored — `subset @ vector`, a plain
   dot product since both sides are pre-normalized — **only over the already-filtered
   candidate indices**, top-`k` by score.
5. **Degraded fallback:** if the embedder is unavailable, or the catalog has no vectors, or
   the query is empty, results are instead ordered price-ascending-then-stock-descending
   (`_fallback_order`) — explainable to a shopper, unlike an arbitrary order — and
   `SearchResult.degraded` is set to a human-readable reason string that the agent is
   expected to say out loud.

---

## 10. Discovery tools — `app/tools/`

All four are registered once via the `@tool(...)` decorator in `app/tools/registry.py`,
which emits both OpenAI's and Anthropic's function-schema shapes from the same definition.
Gating is in `app/agent/policy.py::available_tools()`, computed fresh every turn from session
state — never a static list:

| Tool | Gated? | Condition |
|---|---|---|
| `search_catalog` | No | always available |
| `get_product_details` | No | always available |
| `compare_products` | No | always available |
| `build_cart` | No | always available |
| `probe_attributes` | Yes | `session.probe_count < MAX_PROBES_PER_SESSION` |
| `preview_transaction` | Yes | `session.cart.items` non-empty |
| `confirm_and_pay` | Yes | `session.has_valid_confirmation()` (only after the separate `/chat/confirm` POST) |

### `search_catalog` — `app/tools/search_catalog.py`

The workhorse; called before any question and again after every answer.

- **Filter resolution** (`resolve_filters` → `resolve_value`): a free-text filter value from
  the model is mapped onto a real canonical value — exact match, then the field's stored
  alias map from canonicalization, then a fuzzy match (`rapidfuzz.process.extractOne`,
  `WRatio`, threshold **78**). A value that resolves to nothing is **dropped**, not passed
  through — an unresolvable filter would otherwise silently return zero results, which reads
  as a broken assistant, not a query with no matches.
- Numeric filters go through `resolve_numeric()`, which also parses range text like
  `"0.8–1.3"` or `"18 to 36"` into `{min, max}` — the exact shape `probe_attributes` offers
  its numeric bin labels in, since a shopper answers a probe question in that shape.
- **Answered slots persist across turns**: `slots = {**session.known_slots, **filters}` —
  the model cannot accidentally widen a search by simply forgetting to re-send a filter it
  already established.
- **Price bounds are stored as a slot, not a one-off predicate** (`record_price_bounds`),
  and marked `hard` — so the relaxation ladder can never quietly raise a shopper's stated
  budget to hit the minimum-results target.
- `session.last_candidate_ids` is set to the **full filtered candidate set** (before
  top-k), not just what's displayed — this is what `probe_attributes` ranks questions over.
- `summarise_for_model()` sends the model title, price, and up to 4 structured
  `label: value` attribute pairs per card (capped at 8 cards) — never the LLM descriptor
  text, never a full row dump.
- `ctx.products_shown = True` is set here and is the flag `probe_attributes` checks —
  see next.

### `probe_attributes` — `app/tools/probe_attributes.py`

Fully deterministic; ranks *which* question is worth asking, never phrases it.

```
coverage(a) = fraction of the live candidate set with a non-null value for a
H(a)        = normalised Shannon entropy of a's value distribution in that set   (0 = one value dominates, 1 = perfectly even split)
tier_w(a)   = {1: 1.0, 2: 0.6, 3: 0.3}[tier]
score(a)    = tier_w(a) × H(a) × coverage(a)
```

- Operates over `candidate_rows(ctx)` = `session.last_candidate_ids` resolved back to full
  rows — the set `search_catalog` just filtered to, not the whole catalog.
- Numeric fields are quantile-binned into up to 4 bins (`_numeric_bins`) before entropy is
  computed, so entropy reflects the actual shape of the data rather than its raw range.
  Multi-valued categoricals are exploded first, same as the profiler.
- Skip rules (`score_field`): `distinct ≤ 1` (no discriminating power at all),
  `distinct > 12` for non-numeric fields (too many choices to sensibly ask about),
  `coverage < 0.5` (asking would filter away too many real matches on missing data).
- **Structurally refuses to run before `search_catalog` this turn**
  (`if not ctx.products_shown`) — rather than letting the orchestration loop silently
  withhold the question afterwards, it tells the model to search first and call it again.
  This closes a bug found during the build where the loop emitted a probe event and then
  filtered it client-side *after* the frontend had already rendered the question.
- Returns merchant-approved copy (`why_it_matters`, `how_to_find_out`, `probe_question`) per
  candidate but explicitly does not phrase the question — the model rephrases it
  conversationally, which is what keeps the wording fluent while the *selection* stays
  testable and deterministic.
- Budget enforcement (`MAX_PROBES_PER_TURN`, `MAX_PROBES_PER_SESSION`) happens here, and once
  exhausted the tool tells the model to recommend instead of asking further — separately,
  `available_tools()` also withdraws the tool entirely at the session limit.

### `compare_products` — `app/tools/compare_products.py`

Side-by-side table for 2–4 explicit product ids, always available.

- **Axis selection** (`select_axes`) is the whole point of the tool: fields the shopper has
  already spoken about (`known_slots`) come first, then the remainder ordered by merchant
  tier. Price gets a fixed dedicated column. Any axis where **every** compared product has
  the same value is dropped (`differs()`) — a column of four identical "Brand: Bosch" rows
  is noise in a table whose only job is showing the difference; if literally nothing
  differs, the shared attributes are shown anyway rather than an empty table. Capped at 6
  axes.
- Returns a `ComparisonEvent` (structured `{axes, rows}` for the frontend to render as a
  real table) plus a text version for the model that ends with an explicit instruction:
  *"Summarise the real trade-off between these in a sentence. Do not read the table out."*

### `get_product_details` — `app/tools/get_product_details.py`

Full-detail lookup for one product id — "pull everything known about this item," used when
the shopper asks about something specific.

- `product_card(..., max_attributes=20)` — every other card-producing path caps at 4
  attributes; this is the one place the full attribute list (up to 20) is returned.
- Explicitly flags `"OUT OF STOCK — do not recommend this"` inline in the text sent to the
  model when relevant, rather than relying on the model to infer it from a stock number.

### Recommending is not a tool

There is no `recommend` action. A recommendation is just the chat model's own next reply
after reading the output of the tools above, governed by system-prompt instruction
("recommend with a reason tied to what they told you, not a spec sheet") rather than a gated
capability — unlike `confirm_and_pay`, a recommendation has no real-world side effect, so
there is nothing here that needs a structural gate.

---

## 11. Known limitation: descriptor quality on near-duplicate products

The enrichment prompt (§7) is generic and batched — for a cluster of near-identical products
(five cordless drills differing only in chuck size and voltage), it will plausibly produce
five very similar descriptor sentences, weakening the *vector-ranking* discrimination
between them. This is a real, accepted gap, contained deliberately to one place:

- `build_document()` still concatenates the raw attribute phrases (`"Chuck size: 13 mm"`)
  alongside the descriptor, so the discriminating information is present in the embedded
  text even when the descriptor isn't — it's just that numbers-as-text embed weakly compared
  to natural language.
- Fine discrimination between similar products is delegated to two paths that never touch
  the descriptor or the embedding at all: `search_catalog`'s exact predicate filters, and
  `probe_attributes`'s entropy ranking over raw column values. Vector search's actual job is
  wide recall on vague/paraphrased intent, not precise numeric separation.
- The descriptor is **never sent back to the LLM** at generation time — `summarise_for_model`
  and `product_card` both build their text from structured `FieldSpec` values, not the
  descriptor string. A generic descriptor can make initial ranking order among duplicates
  somewhat arbitrary; it cannot make the model's eventual recommendation vaguer, because the
  model never sees it.

---

## Config knobs relevant to this subsystem

| Var | Default | Effects |
|---|---|---|
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` \| `openai` — swaps `get_embedder()` |
| `OLLAMA_BASE_URL` / `OLLAMA_EMBED_MODEL` | `http://localhost:11434` / `bge-m3`* | *actual default is `nomic-embed-text`, 768-dim, per `PROGRESS.md` |
| `embed_timeout_seconds` | — | hard cap on both index-build and query embedding calls |
| `SEARCH_MIN_RESULTS` | `3` | relaxation ladder target |
| `SEARCH_TOP_K` | `6` | results returned per search |
| `MAX_PROBES_PER_TURN` / `MAX_PROBES_PER_SESSION` | `1` / `4` | probe budget, enforced in both `probe_attributes` and `policy.available_tools` |

## File map

```
app/ingestion/
  loader.py          phase 1 — load
  coerce.py           phase 2 — coerce
  profiler.py          phase 3 — profile
  schema_map.py         phase 3 — role mapping
  canonicalize.py        phase 4 — canonicalize
  classify.py             phase 5 — classify
  bootstrap.py              assembles the deterministic profile (phases 1-3 output)
  pipeline.py                orchestrates analyze_file / analyze_rows / enrich_with_llm
  merge.py                    re-ingest edit-preservation (not covered above)
  profile_store.py              versioned persistence

app/retrieval/
  enrich.py            phase 7 — LLM descriptors
  index.py               phase 8 — document build, embed, persist/load
  search.py                phase 9 — filter, rank, degrade
  filters.py                 predicates + relaxation ladder
  sync.py                      pulls raw rows from Merchant Backend (not covered above)
  registry.py                   multi-merchant in-memory index registry (not covered above)

app/embeddings/
  base.py             provider protocol, prefixing, L2 normalize
  ollama_provider.py    default provider
  openai_provider.py     swap target
  factory.py               get_embedder(), embedding_model_id()

app/tools/
  search_catalog.py    retrieve
  probe_attributes.py    ask
  compare_products.py      compare
  get_product_details.py     inspect
  registry.py                   @tool decorator, schema emission
```
