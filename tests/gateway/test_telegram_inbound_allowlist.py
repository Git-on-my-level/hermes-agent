"""Tests for Telegram inbound target allowlists."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


class TestParseAllowedInboundTargets:
    def test_supports_string_and_dict_forms(self):
        parsed = TelegramAdapter._parse_allowed_inbound_targets(
            [
                "telegram:-100111:7",
                "-100222:9",
                {"chat_id": -100333, "thread_id": 11},
                {"chat_id": -100444},
                -100555,
            ]
        )

        assert parsed == {
            ("-100111", "7"),
            ("-100222", "9"),
            ("-100333", "11"),
            ("-100444", None),
            ("-100555", None),
        }

    def test_none_and_bool_values_are_ignored(self):
        parsed = TelegramAdapter._parse_allowed_inbound_targets([None, True, False, "", {"thread_id": 7}])
        assert parsed == set()


class TestInboundTargetFiltering:
    def _make_adapter(self, extra=None):
        config = PlatformConfig(enabled=True, token="***")
        if extra:
            config.extra.update(extra)
        return TelegramAdapter(config)

    def test_empty_allowlist_allows_everything(self):
        adapter = self._make_adapter()
        assert adapter._is_inbound_target_allowed(chat_id="-1001", chat_type="group", thread_id="7") is True

    def test_matching_thread_is_allowed(self):
        adapter = self._make_adapter(
            {"allowed_inbound_targets": ["-1001:7"]}
        )
        assert adapter._is_inbound_target_allowed(chat_id="-1001", chat_type="group", thread_id="7") is True
        assert adapter._is_inbound_target_allowed(chat_id="-1001", chat_type="group", thread_id="8") is False
        assert adapter._is_inbound_target_allowed(chat_id="-1002", chat_type="group", thread_id="7") is False

    def test_chat_level_entry_allows_all_topics_in_chat(self):
        adapter = self._make_adapter(
            {"allowed_inbound_targets": [{"chat_id": -1001}]}
        )
        assert adapter._is_inbound_target_allowed(chat_id="-1001", chat_type="group", thread_id="7") is True
        assert adapter._is_inbound_target_allowed(chat_id="-1001", chat_type="group", thread_id="99") is True
        assert adapter._is_inbound_target_allowed(chat_id="-1002", chat_type="group", thread_id="7") is False

    def test_dms_remain_allowed_even_with_group_allowlist(self):
        adapter = self._make_adapter(
            {"allowed_inbound_targets": ["-1001:7"]}
        )
        assert adapter._is_inbound_target_allowed(chat_id="693180290", chat_type="dm", thread_id=None) is True


class TestInboundHandlers:
    @pytest.mark.asyncio
    async def test_handle_text_message_ignores_disallowed_target(self, monkeypatch):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
        monkeypatch.setattr(adapter, "_is_inbound_message_allowed", lambda _message: False)
        enqueue = MagicMock()
        monkeypatch.setattr(adapter, "_enqueue_text_event", enqueue)
        build = MagicMock()
        monkeypatch.setattr(adapter, "_build_message_event", build)

        update = SimpleNamespace(message=SimpleNamespace(text="hello"))
        await adapter._handle_text_message(update, None)

        enqueue.assert_not_called()
        build.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_command_ignores_disallowed_target(self, monkeypatch):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
        monkeypatch.setattr(adapter, "_is_inbound_message_allowed", lambda _message: False)
        handle = AsyncMock()
        monkeypatch.setattr(adapter, "handle_message", handle)
        build = MagicMock()
        monkeypatch.setattr(adapter, "_build_message_event", build)

        update = SimpleNamespace(message=SimpleNamespace(text="/status"))
        await adapter._handle_command(update, None)

        handle.assert_not_awaited()
        build.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_media_message_ignores_disallowed_target(self, monkeypatch):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
        monkeypatch.setattr(adapter, "_is_inbound_message_allowed", lambda _message: False)
        handle = AsyncMock()
        monkeypatch.setattr(adapter, "handle_message", handle)
        build = MagicMock()
        monkeypatch.setattr(adapter, "_build_message_event", build)

        update = SimpleNamespace(message=SimpleNamespace(photo=None, video=None, audio=None, voice=None, document=None, sticker=None))
        await adapter._handle_media_message(update, None)

        handle.assert_not_awaited()
        build.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_location_message_ignores_disallowed_target(self, monkeypatch):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
        monkeypatch.setattr(adapter, "_is_inbound_message_allowed", lambda _message: False)
        handle = AsyncMock()
        monkeypatch.setattr(adapter, "handle_message", handle)

        update = SimpleNamespace(
            message=SimpleNamespace(
                venue=None,
                location=SimpleNamespace(latitude=1.0, longitude=2.0),
            )
        )
        await adapter._handle_location_message(update, None)

        handle.assert_not_awaited()
