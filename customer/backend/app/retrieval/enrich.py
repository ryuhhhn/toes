"""One LLM descriptor per product, written in customer language.

The biggest single lever on retrieval quality. A catalog says "18 V, 1.1 kg, brushless";
a shopper types "something light I can use up a ladder". Embedding the catalog's own words
cannot bridge that. Embedding a sentence written the way a customer would describe the
product can.

The prompt is category-blind: it receives the derived category and field list at call time
and is never told in advance what kind of thing it is looking at.

Cached by (row hash, profile version) so a re-index only pays for rows that changed, and
fail-open so a descriptor outage degrades retrieval instead of blocking an ingest.
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.config import get_settings
from app.llm.base import LLMClient
from app.llm.factory import LLMUnavailable, get_llm
from app.models.profile import AgentProfile

log = logging.getLogger(__name__)

BATCH_SIZE = 20
MAX_DESCRIPTOR_CHARS = 400
CACHE_FILE = "descriptors.json"

SYSTEM_PROMPT = """You write one short sentence per product, describing it the way a \
customer would describe what they need — not the way a catalogue describes what it is.

You are told what kind of products these are and which attributes exist. Use plain words. \
Say who or what it suits, and in what situation it would be the right choice.

Rules:
- One sentence per product, at most 30 words.
- Use only the facts given. Never invent a feature, a material, a certification or a claim.
- No marketing language, no superlatives, no "perfect for everyone".
- Never make a medical, safety, or regulatory claim.

Respond as {"descriptors": {"<product id>": "<sentence>"}}."""


def _cache_path(merchant_id: str):
    return get_settings().index_dir / merchant_id / CACHE_FILE


def _row_key(row: dict, profile_version: int) -> str:
    payload = json.dumps(row, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{profile_version}:{payload}".encode("utf-8")).hexdigest()
    return digest[:16]


def load_cache(merchant_id: str) -> dict[str, str]:
    path = _cache_path(merchant_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(merchant_id: str, cache: dict[str, str]) -> None:
    path = _cache_path(merchant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write descriptor cache for %s: %s", merchant_id, exc)


def _product_payload(row: dict, profile: AgentProfile) -> dict:
    roles = profile.roles
    payload: dict = {}
    if roles.title:
        payload["name"] = row.get(roles.title)
    for column in roles.text:
        if row.get(column):
            payload["description"] = str(row[column])[:300]
            break
    for spec in profile.active_fields():
        value = row.get(spec.column)
        if value in (None, "", []):
            continue
        label = spec.display_name
        payload[label] = (
            f"{value} {spec.unit}".strip() if spec.unit and not isinstance(value, list) else value
        )
    return payload


async def _describe_batch(
    batch: list[tuple[str, dict]], profile: AgentProfile, llm: LLMClient
) -> dict[str, str]:
    products = {product_id: _product_payload(row, profile) for product_id, row in batch}
    user = (
        f"These are {profile.category}.\n"
        f"Assistant tone: {profile.agent_tone}\n\n"
        f"Products:\n{json.dumps(products, default=str)[:12000]}"
    )

    try:
        response = await llm.complete_json(system=SYSTEM_PROMPT, user=user)
    except Exception as exc:  # noqa: BLE001 - enrichment is an optimisation, never a blocker
        log.warning("descriptor batch failed: %s", exc)
        return {}

    raw = response.get("descriptors")
    if not isinstance(raw, dict):
        return {}

    known = {product_id for product_id, _row in batch}
    return {
        str(product_id): " ".join(str(text).split())[:MAX_DESCRIPTOR_CHARS]
        for product_id, text in raw.items()
        # A descriptor for a product that was not in the batch is a hallucinated row.
        if str(product_id) in known and isinstance(text, str) and text.strip()
    }


async def enrich_rows(
    rows: list[dict], profile: AgentProfile, *, llm: LLMClient | None = None
) -> dict[str, str]:
    """Descriptor per product id. Returns whatever it managed; never raises."""
    id_column = profile.roles.id
    if not id_column:
        return {}

    if llm is None:
        try:
            llm = get_llm()
        except LLMUnavailable:
            log.info("no LLM configured; indexing raw catalog text only")
            return {}

    cache = load_cache(profile.merchant_id)
    descriptors: dict[str, str] = {}
    pending: list[tuple[str, dict]] = []
    keys: dict[str, str] = {}

    for row in rows:
        product_id = str(row.get(id_column))
        key = _row_key(row, profile.version)
        keys[product_id] = key
        cached = cache.get(key)
        if cached:
            descriptors[product_id] = cached
        else:
            pending.append((product_id, row))

    if pending:
        log.info(
            "enriching %d of %d products for %s", len(pending), len(rows), profile.merchant_id
        )

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        produced = await _describe_batch(batch, profile, llm)
        for product_id, text in produced.items():
            descriptors[product_id] = text
            cache[keys[product_id]] = text

    if pending:
        save_cache(profile.merchant_id, cache)

    return descriptors
