"""Final-reply reconciliation for the Telegram commentary preview bubble.

Preview mode keeps interim progress in one editable bubble. Without this
reconciliation the turn's final reply always went out as a fresh send and the
progress bubble stayed orphaned on screen, so a single logical job that wakes
the agent several times (background notices, goal wakeups) scattered 2-3
bubbles per wake. After the final lands, edit the preview bubble in place to
carry the progress stack plus the answer; fall back to the fresh send (which
the caller then performs) when the edit cannot carry the content.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("gateway.stream_consumer")

_FINAL_SEPARATOR = "\n\n───────\n\n"


class _PreviewReconcileHost(Protocol):
    """The GatewayStreamConsumer surface this mixin composes with."""

    cfg: Any
    adapter: Any
    chat_id: str
    _commentary_preview_last_text: str | None
    _commentary_edit_fail_streak: int
    _commentary_preview_is_placeholder: bool
    _commentary_placeholder_sent: bool

    def _metadata_for_send(self, *, final: bool = False, expect_edits: bool = False) -> dict | None: ...

    def _fit_commentary_stack(self, entries: list[str]) -> tuple[str, list[str], bool]: ...

    def _mark_final_delivered(self, record: str | None = None) -> None: ...


class StreamCommentaryPreviewFinalizeMixin:
    """Edit the preview bubble with the turn's final answer (stack preserved)."""

    def preview_final_delivery_confirmed(self) -> bool:
        """True when the preview bubble already carries the turn-final text."""
        return bool(getattr(self, "final_response_sent", False)) and bool(
            getattr(self, "_commentary_preview_message_id", None)
        )

    async def reconcile_preview_with_final(self, final_text: str) -> bool:
        """Edit the preview bubble to progress stack + answer.

        True = the bubble now carries the final; the caller suppresses its
        normal fresh send. False = deliver the final as usual. Never raises.
        """
        try:
            return await self._reconcile_preview_with_final_inner(str(final_text or ""))
        except Exception as e:  # noqa: BLE001 — reconciliation is best-effort
            logger.debug("Preview final reconciliation failed: %s", e)
            return False

    async def _reconcile_preview_with_final_inner(self, final_text: str) -> bool:
        host: _PreviewReconcileHost = self  # type: ignore[assignment]
        if not final_text.strip() or final_text == "(empty)":
            return False
        # Preview mode only; separate mode never had a stacked bubble.
        if str(getattr(host.cfg, "commentary_mode", "separate")).lower() != "preview":
            return False
        message_id = getattr(host, "_commentary_preview_message_id", None)
        if not message_id or not getattr(host, "_commentary_preview_edit_supported", False):
            return False
        # Already delivered through another path (queued-lane double-fire):
        # nothing to fold in, and the caller must not re-send either.
        if getattr(host, "final_response_sent", False):
            return True
        entries = getattr(host, "_commentary_preview_entries", None)
        if getattr(host, "_commentary_preview_is_placeholder", False) or not entries:
            body = final_text
        else:
            body = "\n\n".join(entries) + _FINAL_SEPARATOR + final_text
        body, _entries, fits = host._fit_commentary_stack([body])
        if not fits:
            # The answer alone exceeds an editable bubble: the fresh final
            # send must stand; progress stays as its own breadcrumb bubble.
            return False
        if body == getattr(host, "_commentary_preview_last_text", None):
            host._mark_final_delivered(record=final_text)
            return True
        kwargs: dict[str, Any] = {
            "chat_id": host.chat_id,
            "message_id": message_id,
            "content": body,
            "finalize": True,
        }
        metadata = host._metadata_for_send(expect_edits=True)
        if metadata:
            try:
                import inspect

                params = inspect.signature(host.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                ):
                    kwargs["metadata"] = metadata
            except (TypeError, ValueError):
                pass
        try:
            result = await host.adapter.edit_message(**kwargs)
            if result is not None and not getattr(result, "success", False):
                # One retry, mirroring the interim edit path.
                result = await host.adapter.edit_message(**kwargs)
        except Exception as e:
            if "not modified" in str(e).lower():
                # Bubble already shows exactly this content: success no-op.
                host._commentary_preview_last_text = body
                host._commentary_edit_fail_streak = 0
                host._mark_final_delivered(record=final_text)
                return True
            logger.debug("Preview final reconciliation edit failed: %s", e)
            return False
        if result is None or not getattr(result, "success", False):
            return False
        host._commentary_preview_last_text = body
        host._commentary_edit_fail_streak = 0
        host._commentary_preview_is_placeholder = False
        host._commentary_placeholder_sent = True
        # The bubble now shows the answer: record it as turn-final delivery so
        # every downstream suppression predicate agrees.
        host._mark_final_delivered(record=final_text)
        return True
