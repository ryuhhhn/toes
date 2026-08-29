# Progress Log

Handoff state. Read `CLAUDE.md` for architecture and invariants, `BUILD_PLAN.md` for the
staged execution order. This file records only **what exists**, **what does not**, and
**decisions made during the build that are not yet reflected in the plan documents**.

Last updated: 2026-08-29. Stages A–I complete and verified, J substantially complete.

---

## Status by stage

| Stage | Scope | Status |
|---|---|---|
| A1 | Project skeleton, config, `main.py`, `/health` | **Complete** |
| A2 | LLM provider abstraction | **Complete** — streaming accumulators now tested |
| A3 | Embedding provider abstraction | **Complete** — now `langchain_ollama` + `nomic-embed-text` |
| A4 | Downstream stubs (`stubs/mock_services.py`) | **Complete** |
| B | Ingestion I — loader, coerce | **Complete** |
| C | Ingestion II — profiler, schema_map, bootstrap | **Complete** |
| D | Ingestion III — canonicalize, classify, store, merge, approval API | **Complete** |
| E | Retrieval — sync, enrich, index, registry, filters, search | **Complete** |
| F | Agent core — session, events, tool registry, prompt, loop, SSE | **Complete** |
| G | Discovery tools — search, details, compare, probe | **Complete** |
| H | Commerce tools + trust gate | **Complete** |
| I | Generality validation — fixtures, parameterized suite, anti-hardcoding | **Complete** |
| J | Hardening & observability | **Mostly complete** — see gaps below |
| K | Extensions | Runtime cross-field notices done; rest not started |

**299 tests pass** (`uv run pytest`, ~28s, no API key or network needed).
**48/48 live checks pass** (`uv run python scripts/live_check.py`, ~$0.14 per run).

---

## Verification actually performed

Everything below was executed, not assumed.

- `uv sync` and an import smoke test over every module.
- **Streaming accumulators** — the item flagged as least-verified. Now covered by
  `tests/test_llm_streaming.py` against fakes reproducing both SDKs' chunk shapes:
  fragmented tool arguments, two tool calls in one round, the Anthropic tool-result merge.
  Written as permanent tests rather than a throwaway script, so they keep working.
- **Ollama** — reachable, `nomic-embed-text`, 768 dimensions, cosine similarity sane.
- **Live LLM run** against both fixture catalogs: ingest → classify → enrich → index →
  multi-turn conversation → preview → authorise → charge → receipt.

---

## Two requested changes, applied

1. **`langchain_ollama` + `nomic-embed-text`.** `app/embeddings/ollama_provider.py` now
   wraps `OllamaEmbeddings`; `OLLAMA_EMBED_MODEL` defaults to `nomic-embed-text`.
   The asymmetric `search_query:` / `search_document:` prefixes are still applied by *us*
   before the text reaches LangChain, because LangChain sends whatever string it is given.
2. **CSV as a first-class format.** The loader dispatches on content first and extension
   second, handles `.csv/.tsv/.txt/.xlsx/.xls`, sniffs encoding (`utf-8-sig` first — Excel's
   BOM is everywhere) and delimiter, detects the header row beneath junk banner rows, and
   is tested on both a CSV and an XLSX fixture through the same code path.

---

## Bugs found by running the code

Recorded because each was silent, and each would have surfaced during a demo.

1. **`bool("No") is True.** A shopper answering "No" to a yes/no question got the *yes*
   results. Fixed with `coerce.parse_boolean()`, used by both the filter builder and the
   tool; an unparseable answer is dropped rather than inverted.
2. **Numeric probe answers did not filter.** `probe_attributes` offers numeric choices as
   bin labels (`"0.8–1.3"`), and answering with one stored a string on a numeric field,
   which produced no predicate — the shopper watched the count not move. `resolve_numeric()`
   now parses ranges (`-`, `–`, `—`, `to`) into `{min, max}`.
3. **Embeddings silently died across event loops.** `get_embedder()` is `lru_cache`d, so a
   long-lived LangChain client bound its connection pool to the first event loop and failed
   on every later one — surfacing as a *vector-free index*, not an error. The client is now
   constructed per call.
4. **`dtype=str` + `keep_default_na=False` produced the literal string `"nan"`** for empty
   Excel cells, and turned integer SKUs into `"1001.0"`. Excel is read as `dtype=object`
   with an explicit stringify step.
5. **pandas' default NA vocabulary includes `"None"`,** which destroyed a legitimate enum
   level. Missingness is now decided in one place, against one explicit token list.
6. **URLs were split into lists on `/`.** Every image column would have been shredded and
   lost its role. `try_list` now refuses columns that look like URLs.
7. **`identifier` swallowed URL and description columns** (both are all-unique), so no
   catalog ever got an image or text role. Identifiers must now also *look* like codes.
8. **OpenAI rejects `response_format=json_object` unless the word "json" appears** in the
   messages. Cluster labelling and enrichment were both failing silently. Handled in the
   client, and pinned by a test.
9. **A stated budget was forgotten** on the next search. Price bounds are now stored as a
   slot like any other answer, and marked hard so relaxation cannot raise someone's budget.
10. **Products with an unparseable price were recommended** but could not be added to the
    basket — a guaranteed dead end. Now a hard filter, like out-of-stock.
11. **Probe suppression was cosmetic.** The loop emitted probe events and then filtered a
    local list, so the frontend had already rendered the question. `probe_attributes` now
    refuses when nothing has been shown this turn and tells the model to search first;
    the loop keeps a buffering backstop, and a withheld question no longer burns budget.

---

## Decisions made during the build, not in the plan documents

Earlier entries 1–11 from the previous session still hold. Added since:

12. **Deviation from BUILD_PLAN D1, deliberate.** The plan specifies
    `rapidfuzz.token_set_ratio >= 85` for clustering variant spellings. `token_set_ratio`
    scores 100 for any label that contains another as a token subset, so a value and its
    own qualified variants would fuse and silently delete a real distinction.
    `canonicalize.py` uses `fuzz.ratio >= 88` over normalised forms plus a rarity guard, so
    only an *uncommon* variant folds into a common one.

13. **`"none"` is deliberately not a null token.** Blank, `-` and `N/A` are unambiguously
    missing; "None" is frequently a real enum level. Leaking a rare null token into an enum
    is cosmetic, destroying a real value is a correctness bug. `tests/test_generality.py`
    asserts against the same constant, so the two cannot drift.

14. **The stock column is a hidden `FieldSpec`.** Kept so the stock filter can read its
    kind, hidden so it is never probed, compared, embedded or listed to the model.
    "How much stock would you like?" is not a question anyone asks a shopper.

15. **Enum detection accepts a low absolute distinct count** (`<= 12`) as independent
    evidence alongside the cardinality ratio. On a short catalog the ratio is inflated by
    row count alone, and every enum — everything worth asking about — would be lost.

16. **`ToolContext.products_shown` is per-turn state**, which is what lets
    `probe_attributes` enforce "retrieve before you probe" at the source.

17. **Role vocabulary is exempt from the anti-hardcoding guard, attribute vocabulary is
    not.** "retail price" and "units in stock" name the price/stock *roles*, which every
    catalog has and `schema_map.py` must recognise. An attribute name is category
    knowledge. The distinction is documented in `tests/test_anti_hardcoding.py`.

18. **Billed calls are kept out of pytest entirely.** `uv run pytest` is free, offline and
    deterministic; `scripts/live_check.py` is the only thing that spends money, and it
    takes a `--budget` cap and reports estimated spend per phase.

---

## Open questions answered

- **Fixture catalogs chosen** — `fixtures/catalogs/power_tools.csv` (CSV, UTF-8 BOM,
  `snake_case`, `$1,299.00`, unit-bearing numerics, variant brand spellings, an
  unparseable price) and `fixtures/catalogs/tea_and_infusions.xlsx` (XLSX, multi-sheet,
  `Title Case With Spaces`, `1.299,00 €`, list-valued cells with two delimiters, two junk
  rows above the header, an unnamed mostly-empty column). Both have out-of-stock rows.
  Regenerate with `uv run python scripts/make_fixtures.py`.
  Neither is one of the categories named in the design docs — using an illustrative
  example as a target would be the same mistake as tuning to a fixture.
- **`OPENAI_MODEL`** — confirmed as `gpt-4.1` and exercised live. A full live check
  (2 catalogs, ingest + enrich + conversation + purchase) costs about **$0.14**.

---

## Known gaps

- **Stage J**: profile schema-version validation at startup is not implemented (index
  dimension checking is). Token counts are captured for OpenAI but not yet surfaced on
  `GET /trace/{session_id}`; the Anthropic client does not track usage.
- **Anthropic path is untested against the live API** — no key was available. Its streaming
  accumulator and message conversion are unit-tested against fakes, and `complete_json`
  uses the prefill approach, but it has never made a real call.
- **Stage K**: probe-quality eval harness, variant grouping, Redis session store, merchant
  analytics and voice input are all not started.
- **`.env` holds a live OpenAI key.** It is gitignored and `config.py` is its only reader,
  but the key was pasted into a chat transcript — worth rotating after the hackathon.

---

## Immediate next steps

1. `GET /trace/{session_id}` should surface token counts and cost per turn; the OpenAI
   client already accumulates them in `client.usage`.
2. Add usage tracking to the Anthropic client to match, then make one real Anthropic call
   to exercise that path end to end.
3. Stage K1 is the strongest remaining item: the probe-quality eval harness turns "the
   probing feels smart" into a number you can quote to a judge.

---

## Open cross-team items (unchanged, still unsent)

| To | Message |
|---|---|
| Merchant Backend | `GET /catalog/search` cancelled; spec §2.1 LLM field mapping cancelled; we need raw rows, an `ids=` filter, and a post-upload webhook |
| Frontend | SSE event table; `ProductCard` is generic — an `attributes` list, no fixed keys |
| Frontend | Agent Profile shape — they own the merchant approval screen, which is not in their original spec |
| Payment/Auth | We call preview/authorize/confirm/receipt; failure injection is stubbed as `?fail=<code>` |
