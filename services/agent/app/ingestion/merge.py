"""Re-ingest without destroying merchant edits.

A merchant who spent twenty minutes correcting layman copy and marking two fields as
required will not do it twice. So a re-upload diffs against the approved profile:

  * a field the merchant edited keeps its copy and its flags
  * its *measurements* are always refreshed from the new draft — the data changed, and
    stale ranges or stale enum values are worse than no edits at all
  * genuinely new columns arrive as draft
  * vanished columns are marked stale rather than deleted, so a bad export does not
    silently erase configuration
  * re-approval is required only when the field set changed materially
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.profile import AgentProfile, FieldSpec

log = logging.getLogger(__name__)

#: Copy and flags the merchant owns. Preserved for any field listed in edited_fields.
MERCHANT_OWNED = (
    "layman_name",
    "why_it_matters",
    "how_to_find_out",
    "probe_question",
    "tier",
    "required_before_purchase",
    "hidden",
)

#: Measurements. Always taken from the new draft, edited or not.
DETERMINISTIC = (
    "kind",
    "canonical_values",
    "aliases",
    "numeric_min",
    "numeric_max",
    "bins",
    "unit",
    "currency",
    "null_rate",
    "distinct_count",
    "coercion",
)


@dataclass
class MergeReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    requires_reapproval: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_notes(self) -> list[str]:
        notes: list[str] = []
        if self.added:
            notes.append(f"info: new column(s) since last approval: {', '.join(self.added)}")
        if self.removed:
            notes.append(
                f"warning: column(s) missing from this upload, kept but marked stale: "
                f"{', '.join(self.removed)}"
            )
        if self.preserved:
            notes.append(
                f"info: kept your edits on: {', '.join(self.preserved)}"
            )
        if self.requires_reapproval:
            notes.extend(f"warning: re-approval needed — {reason}" for reason in self.reasons)
        return notes


def _merge_field(old: FieldSpec, new: FieldSpec, *, merchant_edited: bool) -> FieldSpec:
    merged = new.model_copy(deep=True)
    if merchant_edited:
        for attribute in MERCHANT_OWNED:
            setattr(merged, attribute, getattr(old, attribute))
    else:
        # Not edited: the merchant's approval of derived copy still stands unless the new
        # draft has something to say. A draft with no copy must not blank out approved copy.
        for attribute in ("layman_name", "why_it_matters", "how_to_find_out", "probe_question"):
            if getattr(merged, attribute) is None:
                setattr(merged, attribute, getattr(old, attribute))
        if new.tier == 2 and old.tier != 2:
            merged.tier = old.tier
        merged.required_before_purchase = old.required_before_purchase
        merged.hidden = old.hidden
    merged.stale = False
    return merged


def merge_profiles(approved: AgentProfile, draft: AgentProfile) -> tuple[AgentProfile, MergeReport]:
    """Fold a fresh draft into an approved profile. Returns the merged profile and a report."""
    report = MergeReport()
    merged = draft.model_copy(deep=True)

    old_fields = {f.column: f for f in approved.fields}
    new_fields = {f.column: f for f in draft.fields}
    edited = set(approved.edited_fields)

    fields: list[FieldSpec] = []
    for column, new_spec in new_fields.items():
        old_spec = old_fields.get(column)
        if old_spec is None:
            report.added.append(column)
            fields.append(new_spec)
            continue

        merchant_edited = column in edited
        fields.append(_merge_field(old_spec, new_spec, merchant_edited=merchant_edited))
        (report.preserved if merchant_edited else report.refreshed).append(column)

    for column, old_spec in old_fields.items():
        if column in new_fields:
            continue
        report.removed.append(column)
        stale = old_spec.model_copy(deep=True)
        stale.stale = True
        fields.append(stale)

    merged.fields = fields
    merged.edited_fields = sorted(edited & set(new_fields))

    # Approved copy that the merchant did not edit is still theirs; keep it unless the
    # new draft actually derived something.
    if draft.derived_by == "bootstrap" and approved.category:
        merged.category = approved.category
        merged.category_confidence = approved.category_confidence
        merged.agent_tone = approved.agent_tone

    merged.cross_field_rules = _merge_rules(approved, draft)

    if report.added:
        report.requires_reapproval = True
        report.reasons.append(f"{len(report.added)} new column(s)")
    if report.removed:
        report.requires_reapproval = True
        report.reasons.append(f"{len(report.removed)} column(s) disappeared")
    if _roles_changed(approved, draft):
        report.requires_reapproval = True
        report.reasons.append("the id, name, price, stock or image column changed")

    merged.status = "draft" if report.requires_reapproval else approved.status
    if not report.requires_reapproval:
        merged.approved_by = approved.approved_by
        merged.approved_at = approved.approved_at

    merged.notes = list(draft.notes) + report.as_notes()
    return merged, report


def _merge_rules(approved: AgentProfile, draft: AgentProfile):
    """An approved rule stays approved; a newly proposed one starts inert."""
    signed = {
        rule.message: rule for rule in approved.cross_field_rules if rule.approved_by_merchant
    }
    merged = []
    seen: set[str] = set()

    for rule in draft.cross_field_rules:
        existing = signed.get(rule.message)
        if existing is not None:
            merged.append(existing.model_copy(deep=True))
        else:
            merged.append(rule.model_copy(deep=True))
        seen.add(rule.message)

    # Keep rules the merchant approved even if this draft did not re-propose them.
    for message, rule in signed.items():
        if message not in seen:
            merged.append(rule.model_copy(deep=True))

    return merged


def _roles_changed(approved: AgentProfile, draft: AgentProfile) -> bool:
    keys = ("id", "title", "price", "stock", "image")
    return any(getattr(approved.roles, k) != getattr(draft.roles, k) for k in keys)
