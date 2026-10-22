"""interim_assistant_message_mode must stay a registered display setting.

A sync that drops the key from _GLOBAL_DEFAULTS leaves YAML working (unknown
settings pass through) but `hermes config set platforms.telegram.interim_assistant_message_mode`
stops redirecting, because only OVERRIDEABLE_KEYS redirect.
"""

from gateway.display_config import (
    OVERRIDEABLE_KEYS,
    _GLOBAL_DEFAULTS,
    _NORMALISERS,
    resolve_display_setting,
)


def test_interim_assistant_message_mode_is_registered():
    assert "interim_assistant_message_mode" in _GLOBAL_DEFAULTS
    assert _GLOBAL_DEFAULTS["interim_assistant_message_mode"] == "separate"
    assert "interim_assistant_message_mode" in OVERRIDEABLE_KEYS
    assert "interim_assistant_message_mode" in _NORMALISERS


def test_interim_assistant_message_mode_normalises_and_resolves_preview():
    cfg = {
        "display": {
            "platforms": {
                "telegram": {"interim_assistant_message_mode": "Preview"},
            }
        }
    }
    assert resolve_display_setting(cfg, "telegram", "interim_assistant_message_mode", "separate") == "preview"
    assert resolve_display_setting({}, "telegram", "interim_assistant_message_mode", "separate") == "separate"
    assert _NORMALISERS["interim_assistant_message_mode"]("nope") == "separate"
