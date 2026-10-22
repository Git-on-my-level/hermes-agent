"""Telegram commentary-preview channel (fork keep-list).

Keep this as a new file + one call site. Do not bury waiting-label wiring
inside ``run.py`` / ``run_turn_runner.py`` control flow that upstream rewrites.
"""

from __future__ import annotations

from typing import Any

from gateway.config import Platform


def format_waiting_label(
    *,
    provider: Any = None,
    model: Any = None,
    reasoning_config: Any = None,
) -> str:
    """Build ``Waiting for provider/model/effort...`` for preview mode."""
    parts: list[str] = []
    provider_s = str(provider or "").strip()
    model_s = str(model or "").strip()
    if provider_s:
        parts.append(provider_s)
    if model_s:
        parts.append(model_s)
    effort_s = ""
    if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is not False:
        effort_s = str(reasoning_config.get("effort") or "").strip()
    if effort_s:
        parts.append(effort_s)
    if not parts:
        return "Waiting for model..."
    return f"Waiting for {'/'.join(parts)}..."


def telegram_preview_channel(
    *,
    platform: Any,
    preview: bool,
    provider: Any = None,
    model: Any = None,
    reasoning_config: Any = None,
) -> tuple[str, str]:
    """Return ``(commentary_mode, waiting_label)`` for ``StreamConsumerConfig``.

    Telegram preview must always carry a non-empty waiting label so the first
    bubble is silent/editable. An empty label is a delayed-ping regression.
    """
    plat = getattr(platform, "value", platform)
    if plat != Platform.TELEGRAM.value or not preview:
        return "separate", ""
    return "preview", format_waiting_label(
        provider=provider, model=model, reasoning_config=reasoning_config,
    )
