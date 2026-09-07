"""Live Telegram preview must seed a waiting label (not just know how to format one)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


def _preview_ctx():
    return TurnContext(
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm"),
        _run_still_current=lambda: True,
        interim_assistant_messages_enabled=True,
        resolve_display_setting=lambda *a, **k: "preview",
        user_config={},
        stream_consumer_holder=[None],
        streaming_tts_consumer_holder=[None],
        event_message_id="msg-1",
        _status_thread_metadata={"thread_id": "topic-1"},
    )


def test_telegram_preview_channel_requires_nonempty_waiting_label():
    from gateway.commentary_preview import telegram_preview_channel

    mode, label = telegram_preview_channel(
        platform=Platform.TELEGRAM,
        preview=True,
        provider="xai-oauth",
        model="grok-4.6",
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    assert mode == "preview"
    assert label == "Waiting for xai-oauth/grok-4.6/medium..."


def test_telegram_preview_channel_off_for_non_telegram():
    from gateway.commentary_preview import telegram_preview_channel

    mode, label = telegram_preview_channel(
        platform=Platform.DISCORD, preview=True, provider="xai-oauth", model="grok-4.6",
    )
    assert mode == "separate"
    assert label == ""


def test_setup_stream_consumer_passes_waiting_label_for_telegram_preview():
    """This is the sync-drop gate. Formatter-only tests still passed when the
    gateway never put the label on StreamConsumerConfig."""
    from gateway.run_turn import GatewayTurnMixin

    class _Runner(GatewayTurnMixin):
        def __init__(self):
            self.config = None
            self._adapter = MagicMock()

        def _adapter_for_source(self, source):
            return self._adapter

    ctx = _preview_ctx()
    runner = TurnRunner(_Runner(), ctx)
    consumer, *_ = runner._setup_stream_consumer(
        "telegram",
        model="grok-4.6",
        provider="xai-oauth",
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    assert consumer is not None
    assert consumer.cfg.commentary_mode == "preview"
    assert consumer.cfg.commentary_waiting_label == "Waiting for xai-oauth/grok-4.6/medium..."
