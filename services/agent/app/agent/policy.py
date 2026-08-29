"""Guardrails, enforced structurally.

The central idea: **a prompt instruction is not a gate; tool absence is.** A model told
not to charge a card will mostly not charge a card. A model with no charging tool in its
schema list cannot.

So the tool list is computed per turn from session state, and `confirm_and_pay` is simply
not in it until the shopper has pressed confirm in a separate HTTP request.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.session.models import Session
from app.tools.registry import ToolDef, all_tools

log = logging.getLogger(__name__)

#: Available in every turn — discovery is never gated.
ALWAYS_AVAILABLE = {
    "search_catalog",
    "get_product_details",
    "compare_products",
    "build_cart",
}

#: Only when the shopper has explicitly authorised this exact cart.
REQUIRES_CONFIRMATION_TOKEN = {"confirm_and_pay"}


def probe_allowed(session: Session) -> bool:
    """An agent that interrogates loses. Past budget, the tool is withdrawn entirely."""
    return session.probe_count < get_settings().max_probes_per_session


def preview_allowed(session: Session, profile=None) -> bool:
    """Whether the shopper can be shown a total yet.

    A non-empty cart is necessary but not sufficient. If the merchant marked any
    field `required_before_purchase` and the shopper has not settled it, there is no
    honest total to show — so the tool is withdrawn rather than the model being asked
    nicely not to call it.

    This module's whole premise is that a prompt instruction is not a gate. The
    required-field rule used to live only in the system prompt, and a model that had
    been told "the merchant requires this to be settled" would cheerfully preview
    anyway when a shopper said "just buy me the first one". `profile` is optional so
    that callers without one (and the existing tests) keep the cart-only behaviour.
    """
    if not session.cart.items:
        return False
    if profile is None:
        return True
    allowed, _ = can_checkout(session, profile)
    return allowed


def available_tools(session: Session, profile=None) -> list[ToolDef]:
    """The tool list for this turn. Never a static list."""
    tools: list[ToolDef] = []

    for tool in all_tools():
        name = tool.name

        if name in REQUIRES_CONFIRMATION_TOKEN:
            if session.has_valid_confirmation():
                tools.append(tool)
            continue

        if name == "probe_attributes":
            if probe_allowed(session):
                tools.append(tool)
            continue

        if name == "preview_transaction":
            if preview_allowed(session, profile):
                tools.append(tool)
            continue

        if name in ALWAYS_AVAILABLE:
            tools.append(tool)

    return tools


def tool_names(session: Session, profile=None) -> list[str]:
    return [t.name for t in available_tools(session, profile)]


def blocking_gaps(session: Session, profile) -> list[str]:
    """Fields the merchant marked required that the shopper has not answered yet.

    Only the merchant can set required_before_purchase, so this list is authorised by a
    human rather than inferred by a model.
    """
    return [
        spec.column
        for spec in profile.blocking_fields()
        if spec.column not in session.known_slots
    ]


def can_checkout(session: Session, profile) -> tuple[bool, str | None]:
    if not profile.roles.is_sellable:
        missing = ", ".join(profile.roles.missing_required())
        return False, f"This catalogue has no {missing}, so it cannot be sold from."
    if not session.cart.items:
        return False, "The basket is empty."

    gaps = blocking_gaps(session, profile)
    if gaps:
        labels = [
            (profile.field(column).display_name if profile.field(column) else column)
            for column in gaps
        ]
        return False, (
            "The merchant requires these to be settled before purchase: "
            + ", ".join(labels)
        )
    return True, None


def describe(session: Session, profile) -> dict:
    """Session-state summary for the system prompt and for /session inspection."""
    allowed, reason = can_checkout(session, profile)
    return {
        "tools": tool_names(session),
        "probes_used": session.probe_count,
        "probe_budget": get_settings().max_probes_per_session,
        "cart_items": len(session.cart.items),
        "cart_subtotal": session.cart.subtotal,
        "has_preview": session.active_preview is not None,
        "preview_valid": session.preview_matches_cart(),
        "authorised": session.has_valid_confirmation(),
        "can_checkout": allowed,
        "checkout_blocked_reason": reason,
    }
