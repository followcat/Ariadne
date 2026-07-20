"""Fastfail / alignment smoke for skills + toolcall design fixes."""

from __future__ import annotations

import pytest

from ariadne.errors import AriadneError
from ariadne.tools.registry import ToolRegistry, ToolSpec


async def _noop(args, ctx):  # noqa: ANN001
    return {"ok": True}


def _spec(name: str, *, required: list[str] | None = None) -> ToolSpec:
    props = {k: {"type": "string"} for k in (required or [])}
    params: dict = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return ToolSpec(
        name=name,
        description="demo",
        parameters=params,
        handler=_noop,
    )


def test_duplicate_tool_register_fastfails() -> None:
    reg = ToolRegistry()
    reg.register(_spec("dup_demo"))
    with pytest.raises(AriadneError) as exc:
        reg.register(_spec("dup_demo"))
    assert exc.value.error.code == "ARIADNE_CONFIG_INVALID"
    reg.register(_spec("dup_demo"), replace=True)


def test_required_args_validated() -> None:
    reg = ToolRegistry()
    reg.register(_spec("needs_x", required=["x"]))
    with pytest.raises(AriadneError) as exc:
        reg.validate_arguments("needs_x", {})
    assert exc.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"
    reg.validate_arguments("needs_x", {"x": "1"})
