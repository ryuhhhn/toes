"""Session and trace inspection.

In a live demo the trace is the only visibility you have into why the agent said what it
said. Being able to read the slots, the candidate set and the tool history for a session,
while it is happening, is the difference between debugging and guessing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.agent.policy import describe
from app.audit import read_all
from app.retrieval.registry import get_registry
from app.session.store import get_session_store

router = APIRouter(tags=["inspect"])


@router.get("/session/{session_id}")
async def get_session(session_id: str) -> dict:
    session = await get_session_store().get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session.")

    index = get_registry().peek(session.merchant_id)
    profile = index.profile if index else None

    return {
        "id": session.id,
        "merchant_id": session.merchant_id,
        "known_slots": session.known_slots,
        "asked_slots": session.asked_slots,
        "declined_slots": session.declined_slots,
        "probe_count": session.probe_count,
        "candidates": len(session.last_candidate_ids),
        "last_shown": session.last_shown_ids,
        "cart": session.cart.model_dump(),
        "cart_hash": session.cart.hash(),
        "preview": session.active_preview.model_dump() if session.active_preview else None,
        "authorised": session.has_valid_confirmation(),
        "policy": describe(session, profile) if profile else None,
        "message_count": len(session.messages),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


@router.get("/trace/{session_id}")
async def get_trace(session_id: str) -> dict:
    """Per-turn tool calls with arguments and latency, plus this session's audit trail."""
    session = await get_session_store().get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session.")

    return {
        "session_id": session_id,
        "tool_calls": [record.model_dump() for record in session.tool_history],
        "total_tool_calls": len(session.tool_history),
        "total_latency_ms": sum(r.latency_ms for r in session.tool_history),
        "audit": [entry for entry in read_all() if entry.get("session") == session_id],
        "messages": session.messages,
    }


@router.get("/sessions")
async def list_sessions() -> dict:
    return {
        "sessions": [
            {
                "id": s.id,
                "merchant_id": s.merchant_id,
                "cart_items": len(s.cart.items),
                "probe_count": s.probe_count,
                "updated_at": s.updated_at,
            }
            for s in get_session_store().all()
        ]
    }
