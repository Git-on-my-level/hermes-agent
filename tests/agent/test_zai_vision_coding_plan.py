"""Regression tests: ZAI vision must target the endpoint the key actually works on.

Before the fix, ``resolve_vision_provider_client``'s ``zai`` branch always built
clients against the hardcoded GENERAL endpoints (_ZAI_OPENAI_VISION_URLS). Z.AI
bills general and Coding Plan balances separately, so a Coding Plan key (e.g.
GLM_API_KEY from the Coding Plan login flow) got 429 error 1113 (insufficient
balance) on every vision call even though the SAME key works on
https://api.z.ai/api/coding/paas/v4. This is why vision_analyze reported "lacks
a vision backend" on GLM-only hosts.

The fix resolves the runtime endpoint (the same one chat traffic uses, via
``resolve_api_key_provider_credentials("zai")``: env override → probe cache in
auth.json → pool entry) and tries it BEFORE the general statics.
"""

from unittest.mock import MagicMock, patch


_CODING_URL = "https://api.z.ai/api/coding/paas/v4"
_GENERAL_ZAI_URL = "https://api.z.ai/api/paas/v4"
_GENERAL_CN_URL = "https://open.bigmodel.cn/api/paas/v4"


def _patch_task_provider():
    """Pin the zai task resolution so tests exercise only the zai branch."""
    return patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("zai", "glm-5v-turbo", None, None, "chat_completions"),
    )


def test_zai_vision_prefers_runtime_resolved_endpoint():
    """A Coding Plan key resolved at runtime must win over the hardcoded general endpoints."""
    from agent.auxiliary_client import resolve_vision_provider_client

    fake_client = MagicMock()
    with _patch_task_provider(), patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        return_value={"provider": "zai", "api_key": "sk-coding", "base_url": _CODING_URL, "source": "GLM_API_KEY"},
    ), patch(
        "agent.auxiliary_client._get_cached_client",
        return_value=(fake_client, "glm-5v-turbo"),
    ) as mock_client:
        provider, client, model = resolve_vision_provider_client(provider="zai")

    assert provider == "zai"
    assert client is fake_client
    assert model == "glm-5v-turbo"
    first_call = mock_client.call_args_list[0]
    assert first_call.kwargs["base_url"] == _CODING_URL


def test_zai_vision_falls_back_to_general_statics_without_runtime_endpoint():
    """No runtime resolution → the old general-endpoint behavior is unchanged."""
    from agent.auxiliary_client import resolve_vision_provider_client

    fake_client = MagicMock()
    with _patch_task_provider(), patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        side_effect=RuntimeError("not configured"),
    ), patch(
        "agent.auxiliary_client._get_cached_client",
        side_effect=[(None, None), (fake_client, "glm-5v-turbo")],
    ) as mock_client:
        provider, client, model = resolve_vision_provider_client(provider="zai")

    assert provider == "zai"
    assert client is fake_client
    tried = [call.kwargs["base_url"] for call in mock_client.call_args_list]
    # Old general-endpoint order preserved: first general static wins when it builds.
    assert tried == [_GENERAL_CN_URL, _GENERAL_ZAI_URL]


def test_zai_runtime_vision_urls_dedupes_and_keeps_statics():
    """The resolved endpoint is tried first; statics stay as fallback and are deduped."""
    from agent.auxiliary_client import _ZAI_OPENAI_VISION_URLS, _zai_runtime_vision_urls

    with patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        return_value={"provider": "zai", "api_key": "sk", "base_url": _GENERAL_ZAI_URL + "/", "source": "GLM_API_KEY"},
    ):
        urls = _zai_runtime_vision_urls()

    assert urls[0] == _GENERAL_ZAI_URL  # trailing slash stripped
    assert urls == (_GENERAL_ZAI_URL,) + tuple(u for u in _ZAI_OPENAI_VISION_URLS if u != _GENERAL_ZAI_URL)
    assert len(urls) == len(set(urls))


def test_zai_runtime_vision_urls_survives_resolution_failure():
    """Resolver raising (no zai credentials) still yields the general statics."""
    from agent.auxiliary_client import _ZAI_OPENAI_VISION_URLS, _zai_runtime_vision_urls

    with patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        side_effect=RuntimeError("boom"),
    ):
        assert _zai_runtime_vision_urls() == _ZAI_OPENAI_VISION_URLS
