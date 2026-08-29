from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.llm.base import (
    AssistantMsg,
    JSONParseError,
    LLMEvent,
    Msg,
    StopReason,
    TextDelta,
    ToolCall,
    ToolResultMsg,
    UserMsg,
    extract_json,
    merge_tool_arguments,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 8192


class AnthropicClient:
    def __init__(self, *, api_key: str, model: str, timeout: float = 90.0):
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)
        self.model = model

    # --- conversion ---------------------------------------------------------

    @staticmethod
    def _to_anthropic(messages: list[Msg]) -> list[dict]:
        """Anthropic carries tool results in *user* messages, and consecutive results
        must share one message rather than arriving as several. Merge as we go."""
        out: list[dict] = []

        for msg in messages:
            if isinstance(msg, UserMsg):
                out.append({"role": "user", "content": [{"type": "text", "text": msg.content}]})

            elif isinstance(msg, AssistantMsg):
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})

            elif isinstance(msg, ToolResultMsg):
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                if msg.is_error:
                    block["is_error"] = True

                # Append to the previous user message if it is already a tool-result batch.
                if (
                    out
                    and out[-1]["role"] == "user"
                    and isinstance(out[-1]["content"], list)
                    and out[-1]["content"]
                    and out[-1]["content"][0].get("type") == "tool_result"
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})

        return out

    # --- streaming ----------------------------------------------------------

    async def stream_with_tools(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": self._to_anthropic(messages),
        }
        if tools:
            payload["tools"] = tools

        current_tool: dict[str, Any] | None = None
        emitted_tool = False
        stop_reason = "end_turn"

        async with self._client.messages.stream(**payload) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "tool_use":
                        current_tool = {
                            "id": block.id,
                            "name": block.name,
                            "fragments": [],
                        }

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        yield TextDelta(delta.text)
                    elif dtype == "input_json_delta" and current_tool is not None:
                        current_tool["fragments"].append(delta.partial_json)

                elif etype == "content_block_stop":
                    if current_tool is not None:
                        yield ToolCall(
                            id=current_tool["id"],
                            name=current_tool["name"],
                            arguments=merge_tool_arguments(current_tool["fragments"]),
                        )
                        emitted_tool = True
                        current_tool = None

                elif etype == "message_delta":
                    reason = getattr(getattr(event, "delta", None), "stop_reason", None)
                    if reason:
                        stop_reason = reason

        yield StopReason("tool_use" if emitted_tool else _normalise_stop(stop_reason))

    # --- structured output --------------------------------------------------

    async def complete_json(
        self, *, system: str, user: str, max_retries: int = 1
    ) -> dict:
        prompt = user
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system + "\n\nRespond with a single valid JSON object and nothing else.",
                messages=[
                    {"role": "user", "content": prompt},
                    # Prefilling the opening brace suppresses preamble prose.
                    {"role": "assistant", "content": "{"},
                ],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            try:
                return extract_json("{" + text if not text.lstrip().startswith("{") else text)
            except JSONParseError as exc:
                last_error = exc
                log.warning("complete_json parse failure (attempt %s): %s", attempt, exc)
                prompt = (
                    f"{user}\n\nYour previous response could not be parsed as JSON "
                    f"({exc}). Respond with a single valid JSON object and nothing else."
                )

        raise last_error or JSONParseError("complete_json failed")


def _normalise_stop(reason: str) -> str:
    return {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "length",
        "stop_sequence": "end_turn",
    }.get(reason, reason)
