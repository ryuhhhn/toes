"""System prompt, assembled from the Agent Profile.

There is not one category word written as a literal anywhere in this file. Every domain
term the agent knows arrives from the approved profile at runtime, which is what lets the
same code sell power tools and loose-leaf tea without a change.

Only merchant-approved cross-field rules are included (invariant 7). An unapproved rule is
a plausible, unverifiable claim, and the agent has no business asserting one.
"""

from __future__ import annotations

from app.models.ingestion import ColumnKind
from app.models.profile import AgentProfile
from app.session.models import Session

MAX_VALUES_LISTED = 12
MAX_FIELDS_DESCRIBED = 20

BEHAVIOUR = """\
How to work:

- Retrieve before you probe. Call search_catalog first, on whatever the shopper has told \
you, however vague. There must be products on screen before you ask anything.
- Pass filters. The moment the shopper tells you something that corresponds to a column \
below, put it in the `filters` argument of search_catalog using that exact column name and \
one of its listed values. Passing filters is what makes the results actually narrow; a \
query string alone does not filter anything. A budget goes in max_price.
- Never ask a bare question. Every question goes alongside results, with a sentence on why \
you are asking.
- Ask at most one question per reply, and only the one probe_attributes ranked as \
worth asking. Phrase it in your own words, conversationally.
- When the shopper answers, search again immediately so they see the set narrow.
- Explain what a field means in plain language. Never assert that a product is suitable \
for a medical, safety or regulatory purpose.
- If you had to relax a constraint to find anything, say so plainly.
- Recommend with a reason tied to what they told you, not to a spec sheet.
- Never say or imply that anything has been bought until you have seen a receipt. Adding \
to the basket and previewing are not purchases.
- The shopper confirms a purchase by pressing the confirm button on the preview card. You \
cannot do it for them, and typing "yes" is not confirmation. If they ask you to just buy \
it, explain that they need to press confirm.
- Keep replies short. Two or three sentences unless asked for more."""


def _field_line(spec) -> str:
    parts = [f'- "{spec.display_name}" (column `{spec.column}`, tier {spec.tier})']

    if spec.kind is ColumnKind.NUMERIC and spec.numeric_min is not None:
        unit = f" {spec.unit}" if spec.unit else ""
        parts.append(f"numeric from {spec.numeric_min:g} to {spec.numeric_max:g}{unit}")
    elif spec.kind is ColumnKind.BOOLEAN:
        parts.append("yes or no")
    elif spec.canonical_values:
        values = ", ".join(f'"{v}"' for v in spec.canonical_values[:MAX_VALUES_LISTED])
        more = "" if len(spec.canonical_values) <= MAX_VALUES_LISTED else ", ..."
        multi = " (a product can have several)" if spec.kind is ColumnKind.CATEGORICAL_MULTI else ""
        parts.append(f"one of: {values}{more}{multi}")

    if spec.why_it_matters:
        parts.append(f"Why it matters: {spec.why_it_matters}")
    if spec.required_before_purchase:
        parts.append("The merchant requires this to be settled before purchase.")

    return " | ".join(parts)


def catalogue_summary(profile: AgentProfile) -> str:
    """The field list the model filters against. Values come from real data only."""
    fields = sorted(profile.active_fields(), key=lambda s: (s.tier, s.column))
    lines = [_field_line(spec) for spec in fields[:MAX_FIELDS_DESCRIBED]]
    return "\n".join(lines) if lines else "- (no attribute columns were derived)"


def approved_rules_block(profile: AgentProfile) -> str:
    rules = profile.approved_rules()
    if not rules:
        return ""
    body = "\n".join(f"- When {r.if_}: {r.message}" for r in rules)
    return (
        "\nThe merchant has approved these specific pieces of advice. You may say these, "
        "and only these, as expert guidance:\n" + body
    )


def build_system_prompt(profile: AgentProfile, session: Session | None = None) -> str:
    currency = ""
    if profile.roles.price:
        spec = profile.field(profile.roles.price)
        if spec and spec.currency:
            currency = f" Prices are in {spec.currency}."

    unapproved = profile.status != "approved"
    caveat = (
        "\nThis catalogue's analysis has not been reviewed by the merchant yet, so be "
        "especially careful not to state anything as expert fact.\n"
        if unapproved
        else ""
    )

    sections = [
        f"You are a shopping assistant for a merchant selling {profile.category}."
        f"{currency}",
        f"Tone: {profile.agent_tone}",
        caveat,
        "What this catalogue records about each product:",
        catalogue_summary(profile),
        approved_rules_block(profile),
        BEHAVIOUR,
    ]

    if session is not None:
        sections.append(_session_block(profile, session))

    return "\n\n".join(section for section in sections if section.strip())


def _session_block(profile: AgentProfile, session: Session) -> str:
    lines = ["Where this conversation has got to:"]

    if session.known_slots:
        known = ", ".join(
            f"{(profile.field(c).display_name if profile.field(c) else c)}={v}"
            for c, v in session.known_slots.items()
        )
        lines.append(f"- They have told you: {known}")
    else:
        lines.append("- They have not told you anything specific yet.")

    unanswered = [c for c in session.asked_slots if c not in session.known_slots]
    if unanswered:
        lines.append(
            "- Already asked and not answered, so do not ask again: "
            + ", ".join(unanswered)
        )

    if session.cart.items:
        basket = ", ".join(f"{i.quantity} x {i.title}" for i in session.cart.items)
        lines.append(
            f"- In the basket: {basket} (subtotal {session.cart.subtotal:g} "
            f"{session.cart.currency}). Nothing is charged."
        )

    if session.active_preview and not session.active_preview.expired:
        lines.append(
            f"- A preview is on screen for {session.active_preview.total:g} "
            f"{session.active_preview.currency}, waiting for them to press confirm."
        )
    if session.has_valid_confirmation():
        lines.append("- They have pressed confirm. Complete the payment now.")

    return "\n".join(lines)
