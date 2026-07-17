from ariadne.tools.registry import build_default_registry


def test_deferred_tools_not_initially_callable() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=True)
    names = {(t.get("function") or {}).get("name") for t in exp.request_tools}
    assert "tool_search" in names
    assert "echo_note" not in names
    assert "echo_note" in exp.deferred_tools
    assert "echo_note" not in exp.callable_function_names
    loaded = exp.load_exact(["echo_note"])
    assert loaded
    assert "echo_note" in exp.callable_function_names
