"""A scripted LLM double.

Drives the real agent loop deterministically and for free. It also records the tool
schemas it was offered on every round, which is what lets the trust-gate tests assert on
tool *absence* rather than on the model's good behaviour.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from app.llm.base import LLMEvent, Msg, StopReason, TextDelta, ToolCall


class ScriptedLLM:
    """Replays a list of turns; each turn is a list of LLMEvents to emit."""

    model = "scripted-test-model"

    def __init__(self, script: list[list[LLMEvent]] | None = None):
        self.script: list[list[LLMEvent]] = list(script or [])
        self.offered_tools: list[list[str]] = []
        self.systems: list[str] = []
        self.message_counts: list[int] = []
        self.json_responses: list[dict] = []

    async def stream_with_tools(
        self, *, system: str, messages: list[Msg], tools: list[dict] | None = None
    ) -> AsyncIterator[LLMEvent]:
        self.systems.append(system)
        self.message_counts.append(len(messages))
        self.offered_tools.append([_tool_name(t) for t in (tools or [])])

        events = self.script.pop(0) if self.script else [TextDelta("All done."),
                                                         StopReason("end_turn")]
        for event in events:
            yield event

    async def complete_json(self, *, system: str, user: str, max_retries: int = 1) -> dict:
        return self.json_responses.pop(0) if self.json_responses else {}

    # --- assertions helpers --------------------------------------------------

    @property
    def last_tools(self) -> list[str]:
        return self.offered_tools[-1] if self.offered_tools else []

    def ever_offered(self, name: str) -> bool:
        return any(name in offered for offered in self.offered_tools)


def _tool_name(schema: dict) -> str:
    if "function" in schema:
        return schema["function"]["name"]
    return schema.get("name", "")


def says(text: str) -> list[LLMEvent]:
    return [TextDelta(text), StopReason("end_turn")]


def calls(name: str, arguments: dict[str, Any] | None = None, *, text: str = "") -> list[LLMEvent]:
    events: list[LLMEvent] = []
    if text:
        events.append(TextDelta(text))
    events.append(ToolCall(id=f"call_{name}", name=name, arguments=arguments or {}))
    events.append(StopReason("tool_use"))
    return events
