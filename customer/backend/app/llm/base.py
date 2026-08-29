"""Provider-neutral LLM interface.

Both SDKs normalise to one event stream so the agent loop never branches on provider.
Adding a third provider means implementing this protocol and nothing else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


# --- streamed events --------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StopReason:
    reason: str  # "end_turn" | "tool_use" | "length" | "error"


LLMEvent = TextDelta | ToolCall | StopReason


# --- neutral message format -------------------------------------------------


@dataclass
class UserMsg:
    content: str


@dataclass
class AssistantMsg:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResultMsg:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


Msg = UserMsg | AssistantMsg | ToolResultMsg


# --- protocol ---------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def stream_with_tools(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]: ...

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_retries: int = 1,
    ) -> dict: ...


# --- shared helpers ---------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class JSONParseError(ValueError):
    pass


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences even when told not to, and a hard failure here
    would fail an entire ingest. Try progressively more forgiving strategies.
    """
    candidates: list[str] = []

    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())

    # Outermost brace span, for responses padded with commentary.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise JSONParseError(f"no JSON object found in response: {text[:400]!r}")


def merge_tool_arguments(fragments: list[str]) -> dict[str, Any]:
    """Both providers stream tool arguments as fragmented JSON. Join, then parse once."""
    raw = "".join(fragments).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
