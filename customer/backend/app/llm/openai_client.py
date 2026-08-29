from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

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


class OpenAIClient:
    def __init__(self, *, api_key: str, model: str, timeout: float = 90.0):
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.model = model
        #: Cumulative token usage for this client, for the trace endpoint and cost control.
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def _record_usage(self, usage) -> None:
        if usage is None:
            return
        self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        self.usage["calls"] += 1

    # --- conversion ---------------------------------------------------------

    @staticmethod
    def _to_openai(messages: list[Msg]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            if isinstance(msg, UserMsg):
                out.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AssistantMsg):
                entry: dict[str, Any] = {"role": "assistant"}
                entry["content"] = msg.content or None
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                out.append(entry)
            elif isinstance(msg, ToolResultMsg):
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
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
            "messages": [{"role": "system", "content": system}, *self._to_openai(messages)],
            "stream": True,
            # Usage arrives in a final chunk with no choices; without this it is lost.
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # index -> {id, name, fragments}; arguments arrive split across chunks.
        pending: dict[int, dict[str, Any]] = {}
        finish_reason = "end_turn"

        stream = await self._client.chat.completions.create(**payload)
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                self._record_usage(chunk.usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta and delta.content:
                yield TextDelta(delta.content)

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = pending.setdefault(
                        tc.index, {"id": "", "name": "", "fragments": []}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["fragments"].append(tc.function.arguments)

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        for _, slot in sorted(pending.items()):
            if not slot["name"]:
                continue
            yield ToolCall(
                id=slot["id"] or f"call_{slot['name']}",
                name=slot["name"],
                arguments=merge_tool_arguments(slot["fragments"]),
            )

        yield StopReason("tool_use" if pending else _normalise_finish(finish_reason))

    # --- structured output --------------------------------------------------

    async def complete_json(
        self, *, system: str, user: str, max_retries: int = 1
    ) -> dict:
        prompt = user
        last_error: Exception | None = None

        # Provider quirk: response_format=json_object is rejected outright unless the word
        # "json" appears somewhere in the messages. Handled here so prompt authors never
        # have to know, and so a prompt that omits it fails loudly in tests, not in a demo.
        instructed = system
        if "json" not in f"{system}{user}".lower():
            instructed = system + "\n\nRespond with a single valid JSON object."

        for attempt in range(max_retries + 1):
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructed},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            self._record_usage(getattr(response, "usage", None))
            text = response.choices[0].message.content or ""
            try:
                return extract_json(text)
            except JSONParseError as exc:
                last_error = exc
                log.warning("complete_json parse failure (attempt %s): %s", attempt, exc)
                prompt = (
                    f"{user}\n\nYour previous response could not be parsed as JSON "
                    f"({exc}). Respond with a single valid JSON object and nothing else."
                )

        raise last_error or JSONParseError("complete_json failed")


def _normalise_finish(reason: str) -> str:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "length",
    }.get(reason, reason)
