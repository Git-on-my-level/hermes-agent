"""External package ownership markers shared by curator selection and write guards."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def external_skill_owner(skill_path: Path | None) -> str | None:
    """Read top-level ``*-managed.json`` (or ``managed.json``) owner markers.

    ``managed_by: hermes`` leaves the skill under Hermes policy. A broken marker
    stays protected using its filename's owner, rather than granting curation.
    Accept SKILL.md paths too, as the candidate scanner works with those.
    """
    if skill_path is None:
        return None
    directory = skill_path.parent if skill_path.name == "SKILL.md" else skill_path
    markers = sorted(set(directory.glob("*-managed.json")) | set(directory.glob("managed.json")))
    for marker in markers:
        fallback = marker.name.removesuffix("-managed.json") if marker.name != "managed.json" else "unknown external owner"
        try:
            data = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            logger.debug("Unreadable skill ownership marker: %s", marker, exc_info=True)
            return fallback
        owner = data.get("managed_by") if isinstance(data, dict) else None
        if not isinstance(owner, str) or not owner.strip():
            return fallback
        if owner.strip().lower() != "hermes":
            return owner.strip()
    return None


def external_owner_message(skill_name: str, owner: str) -> str:
    return (f"Skill '{skill_name}' is managed by {owner}; it is read-only to the curator. "
            "Save lessons in a Hermes-owned local notes or overlay skill instead.")
