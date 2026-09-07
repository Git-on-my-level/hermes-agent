"""Telegram commentary-preview mixin for GatewayStreamConsumer.

Preview mode stacks successive interim commentary into one editable bubble
and seeds an optional waiting placeholder. Separate mode keeps stock
per-item sends. Preview sends set ``_interim_send`` and ``expect_edits``;
Telegram ``reply_to=`` stays off (thread metadata).
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from gateway.platforms.base import BasePlatformAdapter as _BasePlatformAdapter

logger = logging.getLogger("gateway.stream_consumer")


class StreamCommentaryPreviewMixin:
    """Editable stacked commentary bubble (Telegram preview mode)."""

    @staticmethod
    def _commentary_message_ids_from_result(result: Any) -> tuple[str, ...]:
        """Return every concrete message ID exposed by a send/edit result."""
        ids: list[str] = []
        candidates = [getattr(result, "message_id", None)]
        candidates.extend(getattr(result, "continuation_message_ids", None) or ())
        raw = getattr(result, "raw_response", None) or {}
        if isinstance(raw, dict):
            candidates.extend(raw.get("message_ids") or ())
        for message_id in candidates:
            if message_id and str(message_id) != "__no_edit__":
                normalized = str(message_id)
                if normalized not in ids:
                    ids.append(normalized)
        return tuple(ids)

    def _track_commentary_preview_result(self, result: Any) -> tuple[str, ...]:
        """Track every preview bubble created by an adapter fallback/split."""
        message_ids = self._commentary_message_ids_from_result(result)
        for message_id in message_ids:
            if message_id not in self._commentary_preview_message_ids:
                self._commentary_preview_message_ids.append(message_id)
        return message_ids

    @property
    def commentary_preview_message_ids(self) -> tuple[str, ...]:
        """Telegram preview bubbles eligible for post-final cleanup."""
        return tuple(self._commentary_preview_message_ids)

    async def _send_commentary(self, text: str) -> bool:
        """Send a completed interim assistant commentary message.

        In preview mode, successive commentary is stacked into one editable
        bubble (history preserved). A waiting placeholder is replaced by the
        first real entry. Telegram silent-send already suppresses pings unless
        ``metadata[\"notify\"]`` is set; finals set notify separately.
        """
        text = self._clean_for_display(text)
        if not text.strip():
            return False

        preview_mode = (
            str(getattr(self.cfg, "commentary_mode", "separate")).lower()
            == "preview"
        )
        if not preview_mode:
            return await self._deliver_commentary_content(
                text,
                preview_delivery=False,
                preview_mode=False,
                entries=None,
                is_placeholder=False,
            )

        # Build stacked body: first real entry replaces any placeholder;
        # later entries append. Never store the waiting label in the stack.
        if self._commentary_preview_is_placeholder or not self._commentary_preview_entries:
            entries = [text]
        else:
            # De-dupe exact consecutive repeats (common with status thrash).
            if self._commentary_preview_entries[-1] == text:
                return True
            entries = list(self._commentary_preview_entries) + [text]

        body, entries, fits = self._fit_commentary_stack(entries)
        if not fits:
            # Single entry cannot fit an editable bubble — fall back to the
            # adapter full-content send path for this item only.
            #
            # Important: FIFO-rebaseline the stack afterward. If we kept the
            # prior entries while abandoning editable delivery, the *next*
            # normal commentary would rebuild old history + new text and
            # duplicate already-visible content (review on #25).
            ok = await self._deliver_commentary_content(
                text,
                preview_delivery=False,
                preview_mode=True,
                entries=None,
                is_placeholder=False,
            )
            self._commentary_preview_entries = []
            self._commentary_preview_is_placeholder = False
            # Keep any existing preview bubble id so a later normal entry can
            # edit it to just the new text (fresh FIFO stack, same bubble).
            # Always re-enable edits after the out-of-band overlong send.
            self._commentary_preview_edit_supported = True
            return ok

        ok = await self._deliver_commentary_content(
            body,
            preview_delivery=True,
            preview_mode=True,
            entries=entries,
            is_placeholder=False,
        )
        return ok

    def _fit_commentary_stack(
        self, entries: list[str]
    ) -> tuple[str, list[str], bool]:
        """Join stacked commentary and drop oldest entries until under limit.

        Returns ``(body, entries, fits)``. ``fits`` is False only when even the
        newest single entry exceeds the editable limit.
        """
        limit = getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096)
        len_fn: "Callable[[str], int]" = len
        if isinstance(self.adapter, _BasePlatformAdapter):
            try:
                limit = self.adapter.max_message_length_for_chat(self.chat_id)
                len_fn = self.adapter.message_len_fn_for_chat(self.chat_id)
            except Exception:
                pass
        safe_limit = max(1, int(limit) - 100)
        kept = list(entries)
        while kept:
            body = "\n\n".join(kept)
            if len_fn(body) <= safe_limit:
                return body, kept, True
            if len(kept) == 1:
                return body, kept, False
            kept.pop(0)
        return "", [], False

    async def _maybe_send_waiting_placeholder(self) -> bool:
        """Seed the preview bubble with a waiting label if configured."""
        if (
            str(getattr(self.cfg, "commentary_mode", "separate")).lower()
            != "preview"
        ):
            return False
        label = self._clean_for_display(self._commentary_waiting_label)
        if not label.strip():
            return False
        if self._commentary_placeholder_sent or self._commentary_preview_entries:
            return False
        if self._commentary_preview_message_id:
            return False
        return await self._deliver_commentary_content(
            label,
            preview_delivery=True,
            preview_mode=True,
            entries=None,
            is_placeholder=True,
        )

    async def _deliver_commentary_content(
        self,
        text: str,
        *,
        preview_delivery: bool,
        preview_mode: bool,
        entries: list[str] | None,
        is_placeholder: bool,
    ) -> bool:
        """Shared send/edit path for commentary preview and separate modes."""
        if (
            preview_delivery
            and self._commentary_preview_message_id
            and self._commentary_preview_edit_supported
        ):
            if text == self._commentary_preview_last_text:
                return True
            try:
                kwargs: dict[str, Any] = {
                    "chat_id": self.chat_id,
                    "message_id": self._commentary_preview_message_id,
                    "content": text,
                    # Completed commentary should retain Telegram formatting
                    # (rich -> MarkdownV2 -> plain fallback), not use the raw
                    # progressive-edit path.
                    "finalize": True,
                }
                metadata = self._metadata_for_send(expect_edits=True)
                if metadata:
                    try:
                        params = inspect.signature(self.adapter.edit_message).parameters
                        if "metadata" in params or any(
                            param.kind is inspect.Parameter.VAR_KEYWORD
                            for param in params.values()
                        ):
                            kwargs["metadata"] = metadata
                    except (TypeError, ValueError):
                        pass
                result = await self.adapter.edit_message(**kwargs)
                if getattr(result, "success", False):
                    updated_ids = self._track_commentary_preview_result(result)
                    if len(updated_ids) == 1:
                        self._commentary_preview_message_id = updated_ids[0]
                    elif len(updated_ids) > 1:
                        # Overflow created multiple visible messages, so no
                        # single bubble can represent the preview anymore.
                        self._commentary_preview_message_id = None
                        self._commentary_preview_edit_supported = False
                    self._commentary_preview_last_text = text
                    if is_placeholder:
                        self._commentary_preview_is_placeholder = True
                        self._commentary_placeholder_sent = True
                        self._commentary_preview_entries = []
                    else:
                        self._commentary_preview_is_placeholder = False
                        self._commentary_placeholder_sent = True
                        if entries is not None:
                            self._commentary_preview_entries = list(entries)
                    return True
            except Exception as e:
                logger.debug("Commentary preview edit failed: %s", e)

            # Preserve the old bubble as a breadcrumb and degrade this
            # update to a fresh send.  A successful fresh send becomes the
            # next editable preview; all created IDs remain cleanup-eligible.
            self._commentary_preview_edit_supported = False
            self._commentary_preview_message_id = None

        try:
            # Declare interim intent: this send is NOT the turn-final. A
            # stream-is-the-message adapter (relay Slack native streaming)
            # must not let its seal-interception convert this into
            # draft(final=true) — that would seal the live stream with
            # interim text and orphan the true final into a plain-send
            # duplicate (live finding, 2026-08-16 canary).
            _md = self._metadata_for_send(final=False) or {}
            _md["_interim_send"] = True
            # Only pass reply_to for platforms that use reply-anchoring for
            # threading. Discord/Telegram use native thread_id in metadata;
            # passing reply_to on every commentary creates reply spam.
            _plat = getattr(getattr(self.adapter, "platform", None), "value", None)
            _platform_name = str(_plat or getattr(self.adapter, "name", "")).lower()
            _needs_reply_anchor = _platform_name in ("buzz", "slack", "mattermost", "feishu")
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                reply_to=self._initial_reply_to_id if _needs_reply_anchor else None,
                metadata=(
                    {**(self._metadata_for_send(expect_edits=True) or {}), "_interim_send": True}
                    if preview_delivery
                    else _md
                ),
            )
            # Note: do NOT set _already_sent = True here.
            # Commentary messages are interim status updates (e.g. "Using browser
            # tool..."), not the final response. Setting already_sent would cause
            # the final response to be incorrectly suppressed when there are
            # multiple tool calls. See: https://github.com/NousResearch/hermes-agent/issues/10454
            if result.success:
                if preview_delivery:
                    message_ids = self._track_commentary_preview_result(result)
                    if len(message_ids) == 1:
                        self._commentary_preview_message_id = message_ids[0]
                        self._commentary_preview_edit_supported = True
                        self._commentary_preview_last_text = text
                        if is_placeholder:
                            self._commentary_preview_is_placeholder = True
                            self._commentary_placeholder_sent = True
                            self._commentary_preview_entries = []
                        else:
                            self._commentary_preview_is_placeholder = False
                            self._commentary_placeholder_sent = True
                            if entries is not None:
                                self._commentary_preview_entries = list(entries)
                    else:
                        # No ID (or multiple IDs from an adapter split) means
                        # later commentary must stay on the safe fresh-send path.
                        self._commentary_preview_message_id = None
                        self._commentary_preview_edit_supported = False
                # Commentary counts as fresh content — close off any
                # stale tool bubble above it so the next tool starts a
                # new bubble below. Skip for pure waiting placeholders so
                # tool progress is not reset before real work begins.
                if not is_placeholder:
                    self._notify_new_message()
                # Record the exact delivered text so run.py can confirm whether
                # an interim "preview" actually carried the final response, vs.
                # unrelated commentary delivered during a session split (#14238).
                # In preview mode commentary is never final-delivery evidence,
                # even if it happens to equal the final text.  The mode's final
                # answer must always be delivered separately.
                if not preview_mode and not is_placeholder:
                    self._delivered_commentary_texts.append(text)
            return result.success
        except Exception as e:
            logger.error("Commentary send error: %s", e)
            return False
