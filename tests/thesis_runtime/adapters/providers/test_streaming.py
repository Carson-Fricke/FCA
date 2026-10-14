"""Pure parser cases for untrusted provider SSE data and tool fragments."""

import json

import pytest

from thesis_runtime.adapters.providers.invocations import ModelCompletion, ToolCall
from thesis_runtime.adapters.providers.streaming import (
    parse_completion_stream,
    sse_data,
)
from thesis_runtime.contracts.errors import ProviderRejectedError
from thesis_runtime.contracts.output import OutputDelta


def test_stream_assembles_tools_by_index_without_exposing_partial_arguments() -> None:
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning": "Plan",
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "two",
                                "function": {"name": "second", "arguments": "{"},
                            },
                            {
                                "index": 0,
                                "id": "one",
                                "function": {"name": "first", "arguments": '{"x":'},
                            },
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": None,
                                "function": {"name": None, "arguments": "1}"},
                            },
                            {"index": 1, "id": "two", "function": {"arguments": "}"}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
            "id": "generation",
            "provider": "upstream",
        },
    ]
    result = list(
        parse_completion_stream(
            iter([*(json.dumps(event) for event in events), "[DONE]"]),
            provider="openrouter",
            model="test",
            role="primary",
        )
    )
    assert result[:-1] == [OutputDelta("reasoning", "Plan")]
    completion = result[-1]
    assert isinstance(completion, ModelCompletion)
    assert completion.tool_calls == (
        ToolCall("one", "first", '{"x":1}'),
        ToolCall("two", "second", "{}"),
    )
    assert (completion.prompt_tokens, completion.completion_tokens) == (7, 9)
    assert (completion.generation_id, completion.upstream_provider) == (
        "generation",
        "upstream",
    )


def test_sse_decodes_split_unicode_comments_crlf_and_multiline_data() -> None:
    body = ": keepalive\r\ndata: 🧠\r\ndata: second\r\n\r\ndata: [DONE]\r\r".encode()
    assert list(sse_data(iter(bytes([byte]) for byte in body))) == [
        "🧠\nsecond",
        "[DONE]",
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"data: partial",
        b"data: \xff\n\n",
        b"data: \xf0",
        b"data: " + b"x" * (1024 * 1024 + 1),
    ],
    ids=["truncated", "invalid-utf8", "truncated-utf8", "oversized-line"],
)
def test_sse_rejects_truncation_invalid_unicode_and_unbounded_lines(
    body: bytes,
) -> None:
    with pytest.raises(ProviderRejectedError):
        list(sse_data(iter([body])))


@pytest.mark.parametrize(
    "events",
    [
        [],
        ["[DONE]"],
        ["broken JSON"],
        ["[]"],
        ['{"choices":[{"delta":{"content":"partial"}}]}'],
        ['{"error":{"message":"failed"}}'],
        ['{"choices":[{"finish_reason":"error"}]}', "[DONE]"],
        ['{"choices":[{"delta":{"tool_calls":[{"index":-1}]}}]}'],
        ['{"choices":[{"delta":{"content":false}}]}'],
        [
            '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
            '{"choices":[{"delta":{"content":"late"}}]}',
            "[DONE]",
        ],
    ],
)
def test_malformed_or_truncated_stream_never_produces_a_completion(
    events: list[str],
) -> None:
    with pytest.raises(ProviderRejectedError):
        list(
            parse_completion_stream(
                iter(events), provider="openrouter", model="test", role="primary"
            )
        )
