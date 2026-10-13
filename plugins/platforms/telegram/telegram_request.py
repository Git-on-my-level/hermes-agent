"""Keep PTB client and Telegram fallback transport lifetimes aligned."""

from collections.abc import Iterable

import httpx
from telegram.request import HTTPXRequest

from .telegram_network import TelegramFallbackTransport


class TelegramFallbackRequest(HTTPXRequest):
    """A fresh transport belongs to each client, including orphan replacements.

    PTB keeps constructor kwargs across shutdown/initialize and calls
    ``_build_client`` again. Supplying a transport instance in those kwargs
    reuses it after close; an abandoned old client's late close can also
    poison its replacement. Build the transport at the same boundary as
    the client instead. This PTB-private hook is shared by normal rebuilds
    and the adapter's bounded abandoned-close recovery.
    """

    __slots__ = ("_fallback_ips", "_transport_kwargs")

    def __init__(self, fallback_ips: Iterable[str], *, transport_kwargs: dict, **kwargs):
        self._fallback_ips = tuple(fallback_ips)
        self._transport_kwargs = dict(transport_kwargs)
        super().__init__(**kwargs)

    def _build_client(self) -> httpx.AsyncClient:
        kwargs = dict(self._client_kwargs)
        kwargs["transport"] = TelegramFallbackTransport(self._fallback_ips, **self._transport_kwargs)
        return httpx.AsyncClient(**kwargs)
