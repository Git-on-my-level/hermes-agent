"""Telegram inbound client-split detection.

Telegram splits inbound text at 4096 UTF-16 code units. Python ``len()``
counts astral chars as 1, so a maxed first chunk can miss the near-split
delay. Keep this as a new file + thin call sites. Do not bury UTF-16
comparisons inside adapter.py control flow that upstream rewrites.
"""
from gateway.platforms.base import utf16_len

SPLIT_THRESHOLD = 4000


def chunk_len(text: str | None) -> int:
    """UTF-16 code units in *text* — the unit Telegram splits on."""
    return utf16_len(text or "")


def is_near_split(text: str | None, *, threshold: int = SPLIT_THRESHOLD) -> bool:
    """True when this chunk is near Telegram's client-side split point."""
    return chunk_len(text) >= threshold
