"""`hermes chat -Q` must not leak presentation output into stdout (#93220)."""

from __future__ import annotations


def test_suppress_status_output_gates_quiet_tool_messages():
    """The executor's [tool]/[done] fallback must stay silent under -Q.

    ``_should_emit_quiet_tool_messages`` is the gate for the quiet-mode
    KawaiiSpinner fallback in agent/tool_executor.py; with the rendering
    callbacks neutralized it would otherwise print ``[tool]``/``[done]``
    lines straight into -Q's captured stdout (#93220).
    """
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.quiet_mode = True
    agent.tool_progress_callback = None
    agent.platform = "cli"

    agent.suppress_status_output = False
    assert agent._should_emit_quiet_tool_messages() is True

    agent.suppress_status_output = True
    assert agent._should_emit_quiet_tool_messages() is False


def test_suppress_status_output_gates_quiet_spinner(monkeypatch):
    """The raw thinking spinner must stay off under -Q, even on a TTY.

    ``_should_start_quiet_spinner`` gates the raw KawaiiSpinner started once per
    API call by ``announce_api_call`` (agent/turn_iteration_prep.py). ``-Q``
    neutralizes the rendering callbacks, so a PTY stdout (an adapter scraping
    stdout) was the last path still animating spinner frames into the captured
    stdout -Q exists to keep clean (#17).
    """
    import sys

    from run_agent import AIAgent

    agent = object.__new__(AIAgent)

    # The reported topology: PTY stdout, no injected print_fn.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    agent._print_fn = None
    agent.suppress_status_output = False
    assert agent._should_start_quiet_spinner() is True

    agent.suppress_status_output = True
    assert agent._should_start_quiet_spinner() is False

    # An explicit sink does not override the strict gate either.
    agent._print_fn = lambda *_a, **_kw: None
    assert agent._should_start_quiet_spinner() is False
