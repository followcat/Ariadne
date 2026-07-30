from __future__ import annotations

import pytest

from ariadne.context import ContextBlock, ContextCompiler
from ariadne.errors import AriadneError


def test_context_compiler_preserves_required_and_attributes_optional_choices() -> None:
    compiler = ContextCompiler(max_chars=340, min_summary_chars=40)
    blocks = [
        ContextBlock(
            source="kernel",
            role="system",
            content="K" * 80,
            reason="policy",
            required=True,
            trust="kernel",
            verbatim=True,
        ),
        ContextBlock(
            source="high",
            role="system",
            content="H" * 100,
            reason="high score",
            score=10,
            trust="curated",
        ),
        ContextBlock(
            source="low",
            role="system",
            content="L" * 200,
            reason="low score",
            score=1,
            trust="retrieved",
        ),
        ContextBlock(
            source="user",
            role="user",
            content="U" * 40,
            reason="request",
            required=True,
            trust="user",
            verbatim=True,
        ),
    ]

    compiled = compiler.compile(blocks)
    by_source = {item.source: item for item in compiled.attributions}
    assert by_source["kernel"].disposition == "included"
    assert by_source["user"].disposition == "included"
    assert by_source["high"].disposition == "included"
    assert by_source["low"].disposition in {"summarized", "dropped"}
    assert compiled.total_chars <= 340
    assert compiled.messages[0]["content"] == "K" * 80
    assert compiled.messages[-1]["content"] == "U" * 40


def test_context_compiler_fastfails_when_required_evidence_does_not_fit() -> None:
    compiler = ContextCompiler(max_chars=100)
    with pytest.raises(AriadneError) as caught:
        compiler.compile(
            [
                ContextBlock(
                    source="tool_result:c1",
                    role="tool",
                    content="e" * 101,
                    reason="evidence",
                    required=True,
                    trust="tool",
                    verbatim=True,
                    tool_call_id="c1",
                )
            ]
        )
    assert caught.value.error.code == "ARIADNE_CONTEXT_BUDGET_EXCEEDED"


def test_context_compiler_dynamic_tool_evidence_is_never_truncated() -> None:
    compiler = ContextCompiler(max_chars=100)
    compiled = compiler.compile(
        [
            ContextBlock(
                source="kernel",
                role="system",
                content="k" * 70,
                reason="policy",
                required=True,
                trust="kernel",
                verbatim=True,
            )
        ]
    )
    with pytest.raises(AriadneError) as caught:
        compiler.append_required(
            messages=compiled.messages,
            attributions=compiled.attributions,
            block=ContextBlock(
                source="tool_result:c1",
                role="tool",
                content="e" * 31,
                reason="evidence",
                required=True,
                trust="tool",
                verbatim=True,
                tool_call_id="c1",
            ),
        )
    assert caught.value.error.details["source"] == "tool_result:c1"


def test_context_budget_counts_tool_call_arguments_and_tool_schemas() -> None:
    compiler = ContextCompiler(max_chars=180)
    with pytest.raises(AriadneError) as message_error:
        compiler.compile(
            [
                ContextBlock(
                    source="assistant-call",
                    role="assistant",
                    content="",
                    reason="protocol",
                    required=True,
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "large_call",
                                "arguments": "x" * 180,
                            },
                        }
                    ],
                )
            ]
        )
    assert message_error.value.error.code == "ARIADNE_CONTEXT_BUDGET_EXCEEDED"

    with pytest.raises(AriadneError) as schema_error:
        compiler.ensure_request_fits(
            messages=[{"role": "user", "content": "small"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "large_schema",
                        "description": "d" * 180,
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    assert schema_error.value.error.details["tool_schema_chars"] > 180
