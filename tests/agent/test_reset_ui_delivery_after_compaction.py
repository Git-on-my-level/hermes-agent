"""Post-compaction UI delivery state must reopen mid-turn commentary."""

from types import SimpleNamespace

from agent.conversation_compression import reset_ui_delivery_state_after_compaction


def test_reset_clears_delivered_interim_and_stream_tracking():
    calls = []

    class Agent:
        def __init__(self):
            self._delivered_interim_texts = {"checking the repo", "running tests"}
            self._current_streamed_assistant_text = "partial stream"

        def _reset_stream_delivery_tracking(self):
            calls.append("reset_stream")
            self._current_streamed_assistant_text = ""

    agent = Agent()
    reset_ui_delivery_state_after_compaction(agent)

    assert agent._delivered_interim_texts == set()
    assert agent._current_streamed_assistant_text == ""
    assert calls == ["reset_stream"]


def test_reset_is_best_effort_without_stream_helper():
    agent = SimpleNamespace(_delivered_interim_texts={"old"})
    reset_ui_delivery_state_after_compaction(agent)
    assert agent._delivered_interim_texts == set()


def test_interim_dedupe_would_block_without_reset():
    """Document the freeze: identical post-compaction narration is suppressed."""
    delivered = {"checking the repo."}

    def was_delivered(text: str) -> bool:
        return text in delivered

    assert was_delivered("checking the repo.") is True
    delivered.clear()
    assert was_delivered("checking the repo.") is False
