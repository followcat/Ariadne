from ariadne.tools.registry import build_default_registry


def test_deferred_tools_not_initially_callable() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=True)
    names = {(t.get("function") or {}).get("name") for t in exp.request_tools}
    assert "tool_search" in names
    assert "echo_note" not in names
    assert "echo_note" in exp.deferred_tools
    assert "echo_note" not in exp.callable_function_names
    # Real large tools are deferred too (not only demos).
    assert "conversation_state" in exp.deferred_tools
    assert "skill_manage" in exp.deferred_tools
    assert "conversation_state" not in exp.callable_function_names
    loaded = exp.load_exact(["echo_note", "conversation_state"])
    assert {t["function"]["name"] for t in loaded} >= {"echo_note", "conversation_state"}
    assert "echo_note" in exp.callable_function_names
    assert "conversation_state" in exp.callable_function_names


def test_eager_mode_exposes_named_deferred() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=False)
    names = {(t.get("function") or {}).get("name") for t in exp.request_tools}
    assert "conversation_state" in names
    assert "skill_manage" in names
