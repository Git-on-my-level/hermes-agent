"""Preview-bubble retraction for the Telegram commentary preview bubble.

Option-(2) reconciliation: the turn's final reply goes out as its own fresh
bubble (or the turn goes silent), so the interim progress stack must not
linger beside the final — it is deleted when the fresh send is imminent. A
pure silence marker is gated out by the caller (that turn delivers nothing by
design and keeps its progress bubble as the only visible trace), so the
literal marker is never written anywhere.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("gateway.stream_consumer")


class _PreviewRetractHost(Protocol):
    """The GatewayStreamConsumer surface this mixin composes with."""

    adapter: Any
    chat_id: str
    _commentary_preview_message_id: Any
    _commentary_preview_message_ids: list
    _commentary_preview_last_text: str | None
    _commentary_edit_fail_streak: int
    _commentary_preview_entries: list
    _commentary_preview_is_placeholder: bool
    _commentary_placeholder_sent: bool


class StreamCommentaryPreviewFinalizeMixin:
    """Delete the preview bubble when the turn's fresh final is imminent."""

    async def retract_preview_bubbles(self) -> bool:
        """Delete the live preview bubble(s): progress stack + waiting placeholder.

        True when nothing visible remains. Best-effort per bubble: one retry,
        "not found" counts as success (already gone), a refused delete leaves
        the other bubbles still retracted. Never raises.
        """
        try:
            return await self._retract_preview_bubbles_inner()
        except Exception as e:  # noqa: BLE001 — retraction is best-effort
            logger.debug("Preview bubble retraction failed: %s", e)
            return False

    async def _retract_preview_bubbles_inner(self) -> bool:
        host: _PreviewRetractHost = self  # type: ignore[assignment]
        ids: list[str] = []
        for raw in (
            getattr(host, "_commentary_preview_message_id", None),
            *(getattr(host, "_commentary_preview_message_ids", None) or ()),
        ):
            if raw is None or str(raw) == "__no_edit__":
                continue
            mid = str(raw)
            if mid not in ids:
                ids.append(mid)
        ok = True
        for mid in ids:
            if not await self._delete_preview_message(mid):
                ok = False
        self._reset_preview_retract_state()
        return ok

    async def _delete_preview_message(self, message_id: str) -> bool:
        host: _PreviewRetractHost = self  # type: ignore[assignment]
        for attempt in (1, 2):
            try:
                result = await host.adapter.delete_message(host.chat_id, message_id)
            except Exception as e:
                if "not found" in str(e).lower():
                    return True  # already gone — same end state
                if attempt == 2:
                    logger.debug(
                        "Preview bubble delete failed for %s: %s", message_id, e
                    )
                    return False
                continue
            if result is False:
                if attempt == 2:
                    logger.debug("Preview bubble delete refused for %s", message_id)
                    return False
                continue
            return True
        return False

    def _reset_preview_retract_state(self) -> None:
        host: _PreviewRetractHost = self  # type: ignore[assignment]
        host._commentary_preview_message_ids = []
        host._commentary_preview_message_id = None
        host._commentary_preview_last_text = ""
        host._commentary_preview_entries = []
        host._commentary_preview_is_placeholder = False
        host._commentary_placeholder_sent = True  # placeholder, if any, is gone
        host._commentary_edit_fail_streak = 0
