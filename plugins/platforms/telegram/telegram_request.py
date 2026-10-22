"""Keep PTB client and Telegram fallback transport lifetimes aligned."""

import httpx


def build_fallback_request(request_type, *, transport_factory, **kwargs):
    """Build from the adapter's SDK binding, preserving lightweight doubles.

    Real PTB keeps client kwargs across shutdown/initialize. Each client must
    own a fresh transport, including abandoned-close replacements; a late
    old-client close then cannot poison its replacement. Only subclass an
    actual request type with PTB's client-building hook. Optional-SDK mocks
    and request doubles keep their normal constructor/transport contract.
    """

    if not isinstance(request_type, type) or not callable(getattr(request_type, "_build_client", None)):
        return request_type(**kwargs, httpx_kwargs={"transport": transport_factory()})

    class FreshTransportRequest(request_type):
        __slots__ = ()

        def _build_client(self) -> httpx.AsyncClient:
            client_kwargs = dict(self._client_kwargs)
            client_kwargs["transport"] = transport_factory()
            return httpx.AsyncClient(**client_kwargs)

    return FreshTransportRequest(**kwargs)
