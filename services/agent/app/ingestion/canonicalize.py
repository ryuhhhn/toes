"""Variant spellings -> canonical values + an alias map.

Deterministic first: normalise, group exact matches, then fuzzy-merge rare variants into
common ones. The LLM only ever *labels* an existing cluster, and its label is rejected
unless it normalises back to the cluster it was given. It is never asked what values a
column might contain, because it will drop the rare ones and invent plausible ones.

Deviation from BUILD_PLAN D1, deliberate: the plan specifies rapidfuzz token_set_ratio >= 85.
token_set_ratio scores a label 100 against any label that contains it as a token subset, so
any label and a longer label containing it as a token — a value and its own qualified
variants — would fuse into one value and silently delete a real distinction. fuzz.ratio
over normalised forms is used instead, with a rarity guard so only an uncommon variant
folds into a common one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.llm.base import LLMClient
from app.models.ingestion import ColumnKind, ColumnProfile

log = logging.getLogger(__name__)

#: Levenshtein-style similarity over normalised forms. Typos, not near-synonyms.
FUZZY_THRESHOLD = 88

#: A variant only folds into a value that is meaningfully more common than it is.
#: Two equally common similar values are a real distinction, not a misspelling.
RARITY_RATIO = 0.34

CANONICALISABLE_KINDS = {
    ColumnKind.CATEGORICAL_ENUM,
    ColumnKind.CATEGORICAL_MULTI,
    ColumnKind.CATEGORICAL_HIGH_CARD,
}

MAX_CLUSTERS_TO_LABEL = 40

_PUNCT = re.compile(r"[^\w\s]+")
_SPACE = re.compile(r"\s+")


def normalise_value(value: str) -> str:
    """Casefold, drop punctuation, collapse whitespace. Catches most real variants alone."""
    text = _PUNCT.sub(" ", str(value).casefold())
    return _SPACE.sub(" ", text).strip()


@dataclass
class ValueCluster:
    canonical: str
    members: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return sum(self.members.values())

    @property
    def key(self) -> str:
        return normalise_value(self.canonical)


def cluster_values(value_counts: dict[str, int]) -> list[ValueCluster]:
    """Group raw values into clusters, most common first."""
    exact: dict[str, ValueCluster] = {}

    for raw, count in sorted(value_counts.items(), key=lambda item: -item[1]):
        key = normalise_value(raw)
        if not key:
            continue
        if key not in exact:
            # The first (most frequent) spelling seen becomes the representative.
            exact[key] = ValueCluster(canonical=str(raw))
        exact[key].members[str(raw)] = exact[key].members.get(str(raw), 0) + count

    clusters = sorted(exact.values(), key=lambda c: -c.count)

    merged: list[ValueCluster] = []
    for cluster in clusters:
        target = _fuzzy_target(cluster, merged)
        if target is None:
            merged.append(cluster)
            continue
        for raw, count in cluster.members.items():
            target.members[raw] = target.members.get(raw, 0) + count

    return sorted(merged, key=lambda c: -c.count)


def _fuzzy_target(cluster: ValueCluster, existing: list[ValueCluster]) -> ValueCluster | None:
    best: tuple[float, ValueCluster] | None = None
    for candidate in existing:
        # Only a rarer variant folds into a commoner one.
        if cluster.count > max(2, candidate.count * RARITY_RATIO):
            continue
        score = fuzz.ratio(cluster.key, candidate.key)
        if score >= FUZZY_THRESHOLD and (best is None or score > best[0]):
            best = (score, candidate)
    return best[1] if best else None


def clusters_to_mapping(clusters: list[ValueCluster]) -> tuple[list[str], dict[str, str]]:
    canonical_values = [c.canonical for c in clusters]
    aliases: dict[str, str] = {}
    for cluster in clusters:
        for raw in cluster.members:
            if raw != cluster.canonical:
                aliases[raw] = cluster.canonical
    return canonical_values, aliases


def canonicalize_profile(profile: ColumnProfile) -> tuple[list[str], dict[str, str]]:
    """Deterministic canonicalization for one column. Always safe to run."""
    if profile.kind not in CANONICALISABLE_KINDS:
        return [], {}
    return clusters_to_mapping(cluster_values(profile.value_counts))


# --- optional LLM labelling --------------------------------------------------

_LABEL_SYSTEM = (
    "You tidy up the display spelling of values that were extracted from a product "
    "catalogue. You are given clusters of raw spellings that have already been grouped. "
    "For each cluster, return the single best display spelling.\n\n"
    "Rules:\n"
    "- Choose or re-case one of the spellings given. Fix capitalisation and spacing only.\n"
    "- Never invent a new word, never translate, never expand an abbreviation.\n"
    "- Never merge clusters and never add clusters.\n"
    'Respond as {"labels": {"<cluster_id>": "<display spelling>"}}.'
)


async def label_clusters(
    clusters: list[ValueCluster], *, column: str, llm: LLMClient
) -> list[ValueCluster]:
    """Ask the model for nicer display spellings, then verify it changed nothing material.

    A label is accepted only if it normalises back to the cluster it was given. That makes
    the step incapable of introducing a value the data never contained.
    """
    if not clusters:
        return clusters

    subset = clusters[:MAX_CLUSTERS_TO_LABEL]
    payload = {
        str(index): sorted(cluster.members, key=lambda m: -cluster.members[m])[:6]
        for index, cluster in enumerate(subset)
    }
    user = (
        f"Column: {column!r}\n"
        f"Clusters of raw spellings:\n{payload}\n\n"
        "Return the best display spelling for each cluster id."
    )

    try:
        response = await llm.complete_json(system=_LABEL_SYSTEM, user=user)
    except Exception as exc:  # noqa: BLE001 - labelling is cosmetic; never fail an ingest
        log.warning("cluster labelling failed for %r: %s", column, exc)
        return clusters

    labels = response.get("labels") or {}
    if not isinstance(labels, dict):
        return clusters

    rejected = 0
    for index, cluster in enumerate(subset):
        label = labels.get(str(index))
        if not isinstance(label, str) or not label.strip():
            continue
        if normalise_value(label) != cluster.key:
            rejected += 1
            continue
        cluster.canonical = label.strip()

    if rejected:
        log.info("rejected %d untraceable label(s) for column %r", rejected, column)

    return clusters


async def canonicalize_profile_with_llm(
    profile: ColumnProfile, *, llm: LLMClient | None
) -> tuple[list[str], dict[str, str]]:
    if profile.kind not in CANONICALISABLE_KINDS:
        return [], {}

    clusters = cluster_values(profile.value_counts)
    if llm is not None:
        clusters = await label_clusters(clusters, column=profile.name, llm=llm)
    return clusters_to_mapping(clusters)
