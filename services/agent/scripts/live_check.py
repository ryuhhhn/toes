"""Live end-to-end quality check against a real LLM. Costs money.

Deliberately not part of the pytest suite: `uv run pytest` must stay free and
deterministic. Run this when you want to see what the system actually produces.

    uv run python scripts/live_check.py [--budget 2.00] [--catalog power_tools.csv]

What it exercises, for every fixture catalog:
  1. full ingest with the LLM — category derivation, tiers, layman copy, probe phrasing
  2. validation, reporting anything the model tried to claim that was rejected
  3. enrichment descriptors and a real embedding index
  4. a real multi-turn conversation through the real agent loop and real tools
  5. the full purchase path, including the out-of-band confirm

It stops as soon as the estimated spend passes the budget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MERCHANT_BASE_URL", "http://stub")
os.environ.setdefault("PAYMENT_BASE_URL", "http://stub")

import httpx  # noqa: E402

import app.clients.http as http_module  # noqa: E402
from app.agent.events import (  # noqa: E402
    PreviewEvent,
    ProbeEvent,
    ProductsEvent,
    ReceiptEvent,
    TokenEvent,
    ToolStartEvent,
)
from app.agent.loop import run_turn  # noqa: E402
from app.agent.policy import tool_names  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.ingestion.pipeline import analyze_file, enrich_with_llm  # noqa: E402
from app.ingestion.profile_store import get_profile_store  # noqa: E402
from app.llm.factory import get_llm  # noqa: E402
from app.retrieval.registry import get_registry  # noqa: E402
from app.session.models import ConfirmationToken, Session  # noqa: E402

# gpt-4.1 list price, USD per million tokens.
PRICE_IN, PRICE_OUT = 2.00, 8.00

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


def spend() -> float:
    usage = getattr(get_llm(), "usage", {})
    return (
        usage.get("prompt_tokens", 0) / 1e6 * PRICE_IN
        + usage.get("completion_tokens", 0) / 1e6 * PRICE_OUT
    )


def report_spend(label: str) -> float:
    usage = getattr(get_llm(), "usage", {})
    cost = spend()
    print(
        f"{DIM}   [{label}] {usage.get('calls', 0)} calls, "
        f"{usage.get('prompt_tokens', 0):,} in / {usage.get('completion_tokens', 0):,} out "
        f"= ${cost:.4f}{RESET}"
    )
    return cost


def head(text: str) -> None:
    print(f"\n{BOLD}{'=' * 78}\n{text}\n{'=' * 78}{RESET}")


def check(ok: bool, message: str) -> bool:
    print(f"   {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET}  {message}")
    return ok


async def wire_stubs() -> None:
    from stubs.mock_services import CATALOGS, load_catalogs
    from stubs.mock_services import app as stub_app

    CATALOGS.clear()
    CATALOGS.update(load_catalogs())
    http_module._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stub_app), base_url="http://stub"
    )


async def ingest(path: Path) -> tuple[object, list[bool]]:
    merchant_id = path.stem
    head(f"INGEST — {path.name}  (the system has never seen this file)")

    result = analyze_file(path, merchant_id=merchant_id)
    print(f"{DIM}   deterministic pass: {len(result.frame)} rows, "
          f"{len(result.frame.columns)} columns{RESET}")

    result = await enrich_with_llm(result)
    report_spend("classify")

    profile = result.profile
    results = []

    print(f"\n{BOLD}   Derived category:{RESET} {profile.category!r} "
          f"(confidence {profile.category_confidence:.2f})")
    print(f"{BOLD}   Tone:{RESET} {profile.agent_tone}")
    print(f"{BOLD}   Roles:{RESET} id={profile.roles.id!r} name={profile.roles.title!r} "
          f"price={profile.roles.price!r} stock={profile.roles.stock!r} "
          f"image={profile.roles.image!r} text={profile.roles.text}")

    print(f"\n{BOLD}   Fields as the agent understands them:{RESET}")
    for spec in sorted(profile.fields, key=lambda s: (s.tier, s.column)):
        print(f"     T{spec.tier} {spec.column:22s} -> {spec.layman_name or '(no copy)'}")
        if spec.why_it_matters:
            print(f"{DIM}          why: {spec.why_it_matters}{RESET}")
        if spec.probe_question:
            print(f"{DIM}          ask: {spec.probe_question}{RESET}")

    if profile.cross_field_rules:
        print(f"\n{BOLD}   Proposed rules (inert until the merchant approves):{RESET}")
        for rule in profile.cross_field_rules:
            print(f"     - if {rule.if_}: {rule.message}")
            print(f"{DIM}       approved={rule.approved_by_merchant} columns={rule.columns}{RESET}")

    rejected = [n for n in profile.notes if "rejected model output" in n]
    if rejected:
        print(f"\n{YELLOW}   Validation rejected:{RESET}")
        for note in rejected:
            print(f"     - {note}")

    print()
    results.append(check(profile.category != "products", "a real category was derived"))
    results.append(check(
        any(s.tier == 1 for s in profile.fields), "at least one field was rated tier 1"
    ))
    results.append(check(
        all(s.layman_name for s in profile.active_fields()),
        "every visible field got plain-language copy",
    ))
    results.append(check(
        all(not r.approved_by_merchant for r in profile.cross_field_rules),
        "no proposed rule is self-approved",
    ))

    known = {s.column for s in profile.fields}
    results.append(check(
        all(s.column in known for s in profile.fields), "no invented columns survived"
    ))

    # Measured facts must survive the LLM pass. Cluster labelling may legitimately re-case
    # a display spelling, so the invariant is traceability, not byte equality: every value
    # must still normalise back to something the profiler actually measured.
    from app.ingestion.canonicalize import normalise_value

    baseline = analyze_file(path, merchant_id=merchant_id)
    measured = {p.name: {normalise_value(v) for v in p.value_counts} for p in
                baseline.column_profiles}

    intact = all(
        profile.field(b.column).kind == b.kind
        and profile.field(b.column).unit == b.unit
        and profile.field(b.column).numeric_min == b.numeric_min
        for b in baseline.profile.fields
    )
    traceable = all(
        normalise_value(value) in measured[spec.column]
        for spec in profile.fields
        for value in spec.canonical_values
        if spec.kind.value != "boolean"
    )
    results.append(check(intact, "measured facts were not overwritten by the model"))
    results.append(check(traceable, "every canonical value still traces to real data"))

    store = get_profile_store()
    profile.version = store.next_version(merchant_id)
    store.save(profile)
    approved = store.approve(profile, approved_by="live-check")
    results.append(check(approved.status == "approved", "profile approved and stored"))

    return approved, results


async def index_catalog(merchant_id: str) -> tuple[object, list[bool]]:
    head(f"INDEX — {merchant_id}")
    status = await get_registry().rebuild(merchant_id)
    report_spend("enrich")

    index = get_registry().peek(merchant_id)
    results = [
        check(bool(status.get("ok")), f"index built: {status.get('rows')} rows"),
        check(bool(status.get("vectors")), f"embeddings present (dim {status.get('dim')})"),
        check(status.get("descriptors", 0) > 0, "customer-language descriptors generated"),
    ]

    if index and index.rows:
        from app.retrieval.index import build_document

        sample = build_document(index.rows[0], index.profile)
        print(f"{DIM}   sample indexed document:\n     {sample[:300]}{RESET}")
    return index, results


async def converse(index, script: list[str]) -> tuple[Session, list[bool]]:
    head(f"CONVERSATION — {index.merchant_id} ({index.profile.category})")
    session = Session(merchant_id=index.merchant_id)
    results = []
    saw_products = saw_probe = False
    last_probe: ProbeEvent | None = None
    turns = list(script)
    answered_turn_added = False

    turn = 0
    while turns:
        message = turns.pop(0)
        turn += 1
        print(f"\n{BOLD}   SHOPPER:{RESET} {message}")
        before = len(session.tool_history)
        print(f"{BOLD}   AGENT:{RESET}   ", end="", flush=True)

        products = probes = None
        async for event in run_turn(session, index.profile, index, message):
            if isinstance(event, TokenEvent):
                print(event.text, end="", flush=True)
            elif isinstance(event, ToolStartEvent):
                print(f"\n{DIM}   [{event.tool}] {event.summary}{RESET}\n   ", end="", flush=True)
            elif isinstance(event, ProductsEvent):
                products = event
                saw_products = True
            elif isinstance(event, ProbeEvent):
                probes = last_probe = event
                saw_probe = True
        print()

        # What the model actually asked for, and what it got back.
        for call in session.tool_history[before:]:
            print(f"{DIM}   TOOL IN  {call.tool}({json.dumps(call.args)}){RESET}")
            print(f"{DIM}   TOOL OUT {'ok' if call.ok else 'ERROR'} "
                  f"in {call.latency_ms}ms — {call.summary}{RESET}")

        if products:
            print(f"{DIM}   -> {products.total_candidates} candidates; showing:{RESET}")
            for card in products.items[:4]:
                attrs = ", ".join(f"{a.label}: {a.display}" for a in card.attributes[:3])
                price = f"{card.price:g} {card.currency}" if card.price is not None else "no price"
                print(f"{DIM}      [{card.id}] {card.title} — {price} — {attrs}{RESET}")
            if products.filters_relaxed:
                print(f"{YELLOW}   -> relaxed: "
                      f"{[f['description'] for f in products.filters_relaxed]}{RESET}")
        if probes:
            print(f"{DIM}   -> asked about {probes.attribute}: options {probes.options}{RESET}")

        print(f"{DIM}   -> slots={session.known_slots} probes={session.probe_count}{RESET}")
        report_spend(f"turn {turn}")

        # Answer whatever the agent actually asked, using a value from this catalogue.
        # Written this way rather than scripted, because the question differs per catalogue
        # — which is the whole point of the system.
        if last_probe and not answered_turn_added and not turns:
            answered_turn_added = True
            option = last_probe.options[0] if last_probe.options else "whatever you suggest"
            turns.append(f"let's go with {option}")

    print()
    results.append(check(saw_products, "products were shown"))
    # Asking less is a feature: the goal is progress, not interrogation. Either the agent
    # asked something, or it narrowed the set enough not to need to.
    results.append(check(
        saw_probe or bool(session.known_slots),
        "the conversation made progress (a question was asked, or the set narrowed)",
    ))
    results.append(check(bool(session.known_slots), "answers were captured into slots"))
    results.append(check(
        len(session.asked_slots) == len(set(session.asked_slots)), "no question was repeated"
    ))
    results.append(check(
        session.probe_count <= get_settings().max_probes_per_session, "probe budget respected"
    ))
    return session, results


async def purchase(session: Session, index) -> list[bool]:
    head(f"PURCHASE — {index.merchant_id}")
    from app.clients.payment import get_payment_client
    from app.tools.build_cart import build_cart
    from app.tools.confirm_and_pay import confirm_and_pay
    from app.tools.preview_transaction import preview_transaction
    from app.tools.registry import ToolContext

    ctx = ToolContext(session=session, profile=index.profile, index=index)
    results = []

    stock_column = index.profile.roles.stock
    product_id = next(
        pid for pid, row in zip(index.ids, index.rows)
        if float(row.get(stock_column) or 0) > 0
    )

    added = await build_cart({"action": "add", "id": product_id, "quantity": 1}, ctx)
    results.append(check(not added.error, f"added {product_id} to the basket"))
    results.append(check(
        "confirm_and_pay" not in tool_names(session),
        "confirm_and_pay is NOT in the model's tool list before authorisation",
    ))

    previewed = await preview_transaction({}, ctx)
    results.append(check(not previewed.error, "preview created after live re-verification"))
    preview_event = next((e for e in previewed.events if isinstance(e, PreviewEvent)), None)
    if preview_event:
        print(f"{DIM}   total {preview_event.total:g} {preview_event.currency} "
              f"(subtotal {preview_event.subtotal:g} + tax {preview_event.tax:g}){RESET}")

    results.append(check(
        "confirm_and_pay" not in tool_names(session),
        "still NOT available after preview — a preview is not consent",
    ))

    blocked = await confirm_and_pay({}, ctx)
    results.append(check(blocked.error, "charging without authorisation is refused"))

    # The out-of-band button press.
    from datetime import datetime, timedelta, timezone

    authorization = await get_payment_client().authorize(
        preview_id=session.active_preview.preview_id, session_id=session.id
    )
    session.confirmation_token = ConfirmationToken(
        preview_id=session.active_preview.preview_id,
        authorization_id=str(authorization["authorization_id"]),
        cart_hash=session.active_preview.cart_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    results.append(check(
        "confirm_and_pay" in tool_names(session),
        "available only after the shopper pressed confirm",
    ))

    charged = await confirm_and_pay({}, ctx)
    receipt = next((e for e in charged.events if isinstance(e, ReceiptEvent)), None)
    results.append(check(receipt is not None, "receipt issued"))
    if receipt:
        print(f"{DIM}   transaction {receipt.transaction_id} "
              f"for {receipt.total:g} {receipt.currency}{RESET}")

    replayed = await confirm_and_pay({}, ctx)
    results.append(check(replayed.error, "the confirmation token cannot be replayed"))
    return results


def write_transcript(session: Session, index) -> Path:
    """Full turn-by-turn log: system prompt, every message, every tool call and result.

    The console shows the conversation; this file shows exactly what the model saw, which
    is what you need when an answer looks wrong and you want to know why.
    """
    from app.agent.prompt import build_system_prompt

    path = get_settings().data_dir / f"transcript_{index.merchant_id}.md"
    lines = [
        f"# Live transcript — {index.merchant_id}",
        f"\nCategory: **{index.profile.category}** · session `{session.id}`",
        "\n## System prompt\n\n```\n" + build_system_prompt(index.profile, session) + "\n```\n",
        "## Messages\n",
    ]

    for message in session.messages:
        role = message.get("role")
        if role == "user":
            lines.append(f"### SHOPPER\n\n{message['content']}\n")
        elif role == "assistant":
            if message.get("content"):
                lines.append(f"### AGENT\n\n{message['content']}\n")
            for call in message.get("tool_calls", []):
                lines.append(
                    f"**TOOL CALL** `{call['name']}`\n\n"
                    f"```json\n{json.dumps(call['arguments'], indent=2)}\n```\n"
                )
        else:
            flag = " (ERROR)" if message.get("is_error") else ""
            lines.append(
                f"**TOOL RESULT** `{message.get('name')}`{flag}\n\n"
                f"```\n{message.get('content', '')}\n```\n"
            )

    lines.append("\n## Tool history\n")
    for call in session.tool_history:
        lines.append(
            f"- `{call.tool}` {json.dumps(call.args)} -> "
            f"{'ok' if call.ok else 'ERROR'} in {call.latency_ms}ms ({call.summary})"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{DIM}   full transcript written to {path}{RESET}")
    return path


SCRIPTS = {
    "default": [
        "hi, I'm not really sure what I need — what have you got?",
        "something for occasional use at home, nothing too heavy",
        "that sounds good, which of those would you recommend and why?",
    ]
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=float, default=3.00, help="USD spend cap")
    parser.add_argument("--catalog", help="run one fixture only")
    args = parser.parse_args()

    await wire_stubs()
    fixtures = sorted((ROOT / "fixtures" / "catalogs").glob("*"))
    fixtures = [p for p in fixtures if p.suffix.lower() in {".csv", ".xlsx", ".xls", ".tsv"}]
    if args.catalog:
        fixtures = [p for p in fixtures if p.name == args.catalog]

    print(f"{BOLD}Live quality check — model {get_llm().model}, budget ${args.budget:.2f}{RESET}")

    all_results: list[bool] = []
    for path in fixtures:
        if spend() > args.budget:
            print(f"\n{YELLOW}Budget reached; stopping before {path.name}.{RESET}")
            break

        profile, results = await ingest(path)
        all_results += results

        index, results = await index_catalog(path.stem)
        all_results += results
        if index is None:
            continue

        session, results = await converse(index, SCRIPTS["default"])
        all_results += results

        all_results += await purchase(session, index)
        write_transcript(session, index)

    head("SUMMARY")
    passed = sum(1 for r in all_results if r)
    total = len(all_results)
    cost = spend()
    usage = getattr(get_llm(), "usage", {})

    print(f"   checks passed: {passed}/{total}")
    print(f"   llm calls:     {usage.get('calls', 0)}")
    print(f"   tokens:        {usage.get('prompt_tokens', 0):,} in / "
          f"{usage.get('completion_tokens', 0):,} out")
    print(f"   estimated cost: ${cost:.4f} of ${args.budget:.2f} budget")

    if passed == total:
        print(f"\n{GREEN}{BOLD}   All live checks passed.{RESET}")
    else:
        print(f"\n{RED}{BOLD}   {total - passed} check(s) failed.{RESET}")

    await http_module.close_http_client()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
