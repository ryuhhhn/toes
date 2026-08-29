"""The agent loop. Which tool fires when lives here, and only here.

Hand-rolled on purpose. A framework would hide the two things that matter most: that the
tool list is recomputed from policy every single round, and that a tool failure becomes a
message the model can recover from rather than an exception that kills the stream.

Per round:
    stream the model -> emit token deltas -> on tool calls, emit tool_start, run them,
    emit their events, append results, re-enter. Capped at MAX_TOOL_ROUNDS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from app.agent.events import (
    DoneEvent,
    ErrorEvent,
    Event,
    NoticeEvent,
    ProbeEvent,
    ProductsEvent,
    TokenEvent,
    ToolStartEvent,
)
from app.agent.policy import available_tools
from app.agent.prompt import build_system_prompt
from app.config import get_settings
from app.llm.base import AssistantMsg, StopReason, TextDelta, ToolCall, ToolResultMsg, UserMsg
from app.llm.factory import LLMUnavailable, get_llm
from app.models.profile import AgentProfile
from app.retrieval.index import CatalogIndex
from app.session.models import Session, ToolCallRecord, new_id
from app.tools.registry import ToolContext, ToolResult, get_tool, schemas_for

log = logging.getLogger(__name__)


async def run_tool(call: ToolCall, ctx: ToolContext) -> ToolResult:
    """Execute one tool. Never raises — a failure is a result the model can read."""
    tool = get_tool(call.name)
    if tool is None:
        return ToolResult.failure(f"There is no tool called {call.name!r}.", code="unknown_tool")

    started = time.perf_counter()
    try:
        result = await tool.handler(call.arguments or {}, ctx)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the conversation
        log.exception("tool %s failed", call.name)
        result = ToolResult.failure(
            f"{call.name} could not complete: {exc}", code="tool_exception"
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    ctx.session.tool_history.append(
        ToolCallRecord(
            tool=call.name,
            args=call.arguments or {},
            latency_ms=latency_ms,
            ok=not result.error,
            summary=result.summary,
        )
    )
    log.info(
        "tool %s args=%s -> %s in %dms",
        call.name,
        call.arguments,
        "error" if result.error else "ok",
        latency_ms,
    )
    return result


def _rule_notices(profile: AgentProfile, session: Session) -> list[Event]:
    """Fire merchant-approved cross-field rules that touch what the shopper has settled.

    Unapproved rules never reach here — approved_rules() filters them — so the agent
    cannot voice a domain claim no human signed off on.
    """
    notices: list[Event] = []
    for rule in profile.approved_rules():
        if rule.columns and set(rule.columns) & set(session.known_slots):
            notices.append(
                NoticeEvent(level=rule.then, message=rule.message, columns=rule.columns)
            )
    return notices


def _rollback_probes(session: Session, probes: list[ProbeEvent]) -> None:
    """A question the shopper never saw must not count against the probe budget."""
    for probe in probes:
        if probe.attribute in session.asked_slots:
            session.asked_slots.remove(probe.attribute)
    session.probe_count = max(0, session.probe_count - len(probes))


async def run_turn(
    session: Session,
    profile: AgentProfile,
    index: CatalogIndex,
    user_message: str | None,
    *,
    turn_id: str | None = None,
) -> AsyncIterator[Event]:
    """Stream one conversational turn as typed events."""
    settings = get_settings()
    turn_id = turn_id or new_id("turn")
    ctx = ToolContext(session=session, profile=profile, index=index)

    if user_message:
        session.append(UserMsg(user_message))

    try:
        llm = get_llm()
    except LLMUnavailable as exc:
        yield ErrorEvent(
            code="llm_unavailable",
            message=f"The assistant is not configured with a language model ({exc}).",
        )
        yield DoneEvent(turn_id=turn_id)
        return

    provider = settings.llm_provider
    pending_probes: list[ProbeEvent] = []
    products_shown = False

    for round_number in range(settings.max_tool_rounds):
        tools = available_tools(session, profile)
        system = build_system_prompt(profile, session)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        stop = "end_turn"

        try:
            async for event in llm.stream_with_tools(
                system=system,
                messages=session.llm_messages(),
                tools=schemas_for(tools, provider),
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    yield TokenEvent(text=event.text)
                elif isinstance(event, ToolCall):
                    calls.append(event)
                elif isinstance(event, StopReason):
                    stop = event.reason
        except asyncio.CancelledError:
            # The shopper closed the tab. Persist what we have and leave cleanly.
            log.info("turn %s cancelled by client disconnect", turn_id)
            raise
        except Exception as exc:  # noqa: BLE001 - keep the stream alive, report inline
            log.exception("model call failed")
            yield ErrorEvent(code="llm_error", message=f"The assistant hit an error: {exc}")
            break

        session.append(AssistantMsg(content="".join(text_parts), tool_calls=calls))

        if not calls:
            break

        allowed = {t.name for t in tools}
        for call in calls:
            if call.name not in allowed:
                # The model asked for a tool policy did not offer this turn. This is the
                # invariant-1 backstop: refuse, and tell it why, rather than running it.
                log.warning("blocked out-of-policy tool call: %s", call.name)
                session.append(
                    ToolResultMsg(
                        tool_call_id=call.id,
                        name=call.name,
                        content=(
                            f"{call.name} is not available right now. The shopper has not "
                            "authorised this step."
                        ),
                        is_error=True,
                    )
                )
                continue

            tool = get_tool(call.name)
            yield ToolStartEvent(
                tool=call.name, summary=(tool.start_summary if tool else "Working")
            )

            result = await run_tool(call, ctx)
            for event in result.events:
                # Probes are held back until we know products were shown this turn.
                # Streaming them immediately would make the rule unenforceable: the
                # frontend would already have rendered the question.
                if isinstance(event, ProbeEvent):
                    pending_probes.append(event)
                    continue
                if isinstance(event, ProductsEvent):
                    products_shown = True
                yield event

            session.append(
                ToolResultMsg(
                    tool_call_id=call.id,
                    name=call.name,
                    content=result.llm_content,
                    is_error=result.error,
                )
            )

        if stop == "end_turn" and not calls:
            break
    else:
        log.info("turn %s hit the tool round cap", turn_id)

    for notice in _rule_notices(profile, session):
        yield notice

    # Never a question without products on screen. Probe-first reads as a form;
    # retrieve-then-probe reads as a salesperson.
    if pending_probes and products_shown:
        for probe in pending_probes:
            yield probe
    elif pending_probes:
        log.warning("withheld %d probe(s): no products were shown this turn", len(pending_probes))
        _rollback_probes(session, pending_probes)
        yield ErrorEvent(
            code="probe_suppressed",
            message="A question was withheld because no products were shown alongside it.",
        )

    session.touch()
    yield DoneEvent(turn_id=turn_id)
