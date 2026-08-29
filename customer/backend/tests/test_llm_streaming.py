"""Streaming accumulator tests.

Both providers fragment tool-call arguments across chunks and both must reassemble them
into one parsed dict. These fakes reproduce the shapes the SDKs actually emit, so the
accumulators are verified without an API key or a billed call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.anthropic_client import AnthropicClient
from app.llm.base import (
    AssistantMsg,
    StopReason,
    TextDelta,
    ToolCall,
    ToolResultMsg,
    UserMsg,
    merge_tool_arguments,
)
from app.llm.openai_client import OpenAIClient


async def _drain(client, **kwargs):
    return [event async for event in client.stream_with_tools(**kwargs)]


# --- OpenAI ------------------------------------------------------------------


def _oa_chunk(*, content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def _oa_tc(index, *, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _fake_openai(chunks):
    async def create(**_kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_openai_accumulates_fragmented_arguments():
    client = OpenAIClient(api_key="test", model="test-model")
    client._client = _fake_openai(
        [
            _oa_chunk(content="Looking"),
            _oa_chunk(content=" that up"),
            _oa_chunk(tool_calls=[_oa_tc(0, id="call_1", name="search_catalog")]),
            _oa_chunk(tool_calls=[_oa_tc(0, arguments='{"query": "warm ')]),
            _oa_chunk(tool_calls=[_oa_tc(0, arguments='jacket", "k": 6}')]),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )

    events = await _drain(client, system="s", messages=[UserMsg("hi")], tools=[{"x": 1}])

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Looking that up"
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "search_catalog"
    assert calls[0].arguments == {"query": "warm jacket", "k": 6}
    assert isinstance(events[-1], StopReason) and events[-1].reason == "tool_use"


async def test_openai_two_tool_calls_in_one_round_stay_separate():
    """Interleaved indices are the case a naive accumulator fuses into one broken call."""
    client = OpenAIClient(api_key="test", model="test-model")
    client._client = _fake_openai(
        [
            _oa_chunk(tool_calls=[_oa_tc(0, id="c1", name="get_product_details")]),
            _oa_chunk(tool_calls=[_oa_tc(1, id="c2", name="get_product_details")]),
            _oa_chunk(tool_calls=[_oa_tc(0, arguments='{"id":')]),
            _oa_chunk(tool_calls=[_oa_tc(1, arguments='{"id":')]),
            _oa_chunk(tool_calls=[_oa_tc(0, arguments=' "A1"}')]),
            _oa_chunk(tool_calls=[_oa_tc(1, arguments=' "B2"}')]),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )

    calls = [e for e in await _drain(client, system="s", messages=[]) if isinstance(e, ToolCall)]

    assert [c.arguments["id"] for c in calls] == ["A1", "B2"]
    assert [c.id for c in calls] == ["c1", "c2"]


async def test_openai_plain_text_turn_reports_end_turn():
    client = OpenAIClient(api_key="test", model="test-model")
    client._client = _fake_openai([_oa_chunk(content="hello"), _oa_chunk(finish_reason="stop")])

    events = await _drain(client, system="s", messages=[])

    assert not [e for e in events if isinstance(e, ToolCall)]
    assert events[-1].reason == "end_turn"


# --- Anthropic ---------------------------------------------------------------


def _an_event(etype, **kwargs):
    return SimpleNamespace(type=etype, **kwargs)


def _fake_anthropic(events):
    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def __aiter__(self):
            for event in events:
                yield event

    return SimpleNamespace(messages=SimpleNamespace(stream=lambda **_kw: Stream()))


async def test_anthropic_accumulates_partial_json():
    client = AnthropicClient(api_key="test", model="test-model")
    client._client = _fake_anthropic(
        [
            _an_event("content_block_start", content_block=SimpleNamespace(type="text")),
            _an_event(
                "content_block_delta", delta=SimpleNamespace(type="text_delta", text="One sec")
            ),
            _an_event("content_block_stop"),
            _an_event(
                "content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="probe"),
            ),
            _an_event(
                "content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"attr'),
            ),
            _an_event(
                "content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='ibute": "x"}'),
            ),
            _an_event("content_block_stop"),
            _an_event("message_delta", delta=SimpleNamespace(stop_reason="tool_use")),
        ]
    )

    events = await _drain(client, system="s", messages=[UserMsg("hi")], tools=[{"x": 1}])

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "One sec"
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert calls[0].id == "toolu_1"
    assert calls[0].arguments == {"attribute": "x"}
    assert events[-1].reason == "tool_use"


async def test_anthropic_two_tool_blocks_emit_two_calls():
    client = AnthropicClient(api_key="test", model="test-model")
    client._client = _fake_anthropic(
        [
            _an_event(
                "content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="t1", name="a"),
            ),
            _an_event(
                "content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"id": "A1"}'),
            ),
            _an_event("content_block_stop"),
            _an_event(
                "content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="t2", name="b"),
            ),
            _an_event(
                "content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"id": "B2"}'),
            ),
            _an_event("content_block_stop"),
            _an_event("message_delta", delta=SimpleNamespace(stop_reason="tool_use")),
        ]
    )

    calls = [e for e in await _drain(client, system="s", messages=[]) if isinstance(e, ToolCall)]

    assert [(c.id, c.arguments["id"]) for c in calls] == [("t1", "A1"), ("t2", "B2")]


def test_anthropic_merges_consecutive_tool_results_into_one_user_message():
    """The API rejects consecutive tool_result blocks split across separate messages."""
    converted = AnthropicClient._to_anthropic(
        [
            UserMsg("find me something"),
            AssistantMsg(
                content="",
                tool_calls=[
                    ToolCall(id="t1", name="a", arguments={}),
                    ToolCall(id="t2", name="b", arguments={}),
                ],
            ),
            ToolResultMsg(tool_call_id="t1", name="a", content="result one"),
            ToolResultMsg(tool_call_id="t2", name="b", content="result two"),
            UserMsg("thanks"),
        ]
    )

    assert [m["role"] for m in converted] == ["user", "assistant", "user", "user"]
    results = converted[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]
    # A genuine user turn after the results must not be swallowed into the batch.
    assert converted[3]["content"][0]["type"] == "text"


def test_anthropic_error_tool_result_flagged():
    converted = AnthropicClient._to_anthropic(
        [ToolResultMsg(tool_call_id="t1", name="a", content="boom", is_error=True)]
    )
    assert converted[0]["content"][0]["is_error"] is True


# --- shared ------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragments,expected",
    [
        ([], {}),
        (['{"a": 1}'], {"a": 1}),
        (['{"a"', ": 1}"], {"a": 1}),
        (['{"a": '], {}),  # truncated stream degrades to empty, never raises
        (["[1,2]"], {}),  # non-object degrades to empty
    ],
)
def test_merge_tool_arguments_never_raises(fragments, expected):
    assert merge_tool_arguments(fragments) == expected


# --- provider quirks ---------------------------------------------------------


async def test_complete_json_injects_the_word_json_when_a_prompt_omits_it():
    """OpenAI rejects response_format=json_object unless "json" appears in the messages.

    This failed silently in every prompt that described its output shape with a literal
    example instead of the word, so it is pinned here rather than left to a live run.
    """
    captured = {}

    class Recorder:
        async def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content='{"ok": true}')
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)], usage=None
            )

    client = OpenAIClient(api_key="test", model="test-model")
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=Recorder())
    )

    await client.complete_json(system='Respond as {"labels": {}}', user="Column: x")

    sent = " ".join(m["content"] for m in captured["messages"]).lower()
    assert "json" in sent
    assert captured["response_format"] == {"type": "json_object"}


async def test_complete_json_leaves_a_prompt_that_already_says_json_alone():
    captured = {}

    class Recorder:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None
            )

    client = OpenAIClient(api_key="test", model="test-model")
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=Recorder()))

    await client.complete_json(system="Return a JSON object.", user="go")

    assert captured["messages"][0]["content"] == "Return a JSON object."
