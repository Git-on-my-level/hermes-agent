"""Budget handoffs use the current tool-result append, never a synthetic user turn."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from hermes_state_continuation import read_checkpoint, save_checkpoint

WRAPUP_NOTICE = (
    "[Session handoff requested] The soft iteration budget has been reached. "
    "Wrap up now: report the original objective, completed work with evidence, exact next "
    "steps, output file paths, and an inventory of uncommitted work. Use the remaining "
    "tools only to inspect/checkpoint that state; do not start new work. Your final reply "
    "will be saved as a handoff for a fresh continuation session."
)


@dataclass(frozen=True)
class ContinuationPolicy:
    enabled: bool = False
    max_per_origin: int = 3
    soft_budget_fraction: float = 0.9

    @classmethod
    def from_config(cls, config):
        raw = config.get("continuation", {})
        if not isinstance(raw, dict):
            raise ValueError("continuation must be a mapping")
        enabled = raw.get("enabled", False)
        maximum = raw.get("max_per_origin", 3)
        fraction = raw.get("soft_budget_fraction", 0.9)
        if not isinstance(enabled, bool):
            raise ValueError("continuation.enabled must be boolean")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise ValueError("continuation.max_per_origin must be a nonnegative integer")
        if (isinstance(fraction, bool) or not isinstance(fraction, (float, int))
                or not math.isfinite(fraction) or not 0 < fraction < 1):
            raise ValueError("continuation.soft_budget_fraction must be between 0 and 1")
        return cls(enabled, maximum, float(fraction))


def load_policy():
    from hermes_cli.config_effective import load_user_config_effective
    return ContinuationPolicy.from_config(load_user_config_effective())


def progress_handoff(agent, messages, objective=None, summary=None):
    """Preserve observed progress; file paths are not a claim that changes were committed."""
    state = read_checkpoint(agent._session_db, agent.session_id) if getattr(agent, "_session_db", None) else {}
    if state.get("root"):
        root_handoff = read_checkpoint(agent._session_db, state["root"]).get("handoff")
        if root_handoff:
            objective = json.loads(root_handoff).get("objective")
    objective = objective or next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), "Unknown objective")
    todos = getattr(agent, "_todo_store", None)
    paths = sorted(getattr(agent, "_turn_file_mutation_paths", None) or [])
    # Never replay a previous session's wrap-up directive in the new session seed.
    # The original objective is stored separately; omitting user rows avoids nesting
    # the full previous seed at every hop.
    recent = []
    for message in messages[-12:]:
        if message.get("role") not in {"assistant", "tool"}:
            continue
        row = {k: message[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in message}
        content = row.get("content")
        if isinstance(content, str):
            row["content"] = content.replace(WRAPUP_NOTICE, "")[-8000:]
        elif isinstance(content, list):
            row["content"] = [part for part in content if part.get("text") != WRAPUP_NOTICE]
        recent.append(row)
    return json.dumps({
        "objective": objective, "state_and_next_steps": summary or "Interrupted; inspect recent progress before resuming.",
        "step_markers": todos.read() if todos is not None else [],
        "output_files_and_uncommitted_candidates": paths,
        "inventory_status": "Observed paths only; verify git status and output existence on the owning backend.",
        "recent_progress": recent,
    }, ensure_ascii=False, default=str)


def maybe_request_handoff(agent, messages):
    policy = getattr(agent, "continuation_policy", ContinuationPolicy())
    if (policy.enabled is not True or getattr(agent, "_continuation_wrapup", False) is True
            or getattr(agent, "_interrupt_requested", False) or getattr(agent, "_persist_disabled", False)
            or not getattr(agent, "_session_db", None)):
        return False
    budget = agent.iteration_budget
    maximum = min(agent.max_iterations, budget.max_total)
    used = max(getattr(agent, "_api_call_count", 0), budget.used)
    if maximum <= 1 or used < min(math.ceil(maximum * policy.soft_budget_fraction), maximum - 1):
        return False
    from agent.context_compressor import _DB_PERSISTED_MARKER
    if not messages or messages[-1].get("role") != "tool" or messages[-1].get(_DB_PERSISTED_MARKER):
        return False
    tail = messages[-1]
    content = tail.get("content")
    if not isinstance(content, (str, list)) and content is not None:
        return False
    save_checkpoint(agent._session_db, agent.session_id, progress_handoff(agent, messages), "soft_budget")
    tail["content"] = (content + "\n\n" + WRAPUP_NOTICE if isinstance(content, str)
                       else [*(content or []), {"type": "text", "text": WRAPUP_NOTICE}])
    agent._continuation_wrapup = True
    return True


def finish_handoff(agent, messages, summary, reason):
    handoff = progress_handoff(agent, messages, summary=summary)
    save_checkpoint(agent._session_db, agent.session_id, handoff, reason)
    agent._continuation_ready = True
    surface = " in this profile's Hermes CLI" if getattr(agent, "platform", None) == "cron" else ""
    return (summary or "Progress checkpoint saved.") + (
        f"\n\nSession checkpoint saved. Resume{surface} with /resume {agent.session_id} and ask to continue."
    )


def budget_handoff(agent, messages, final_response, api_call_count, interrupted, failed, exit_reason):
    """The hard backstop never makes an extra model call for a checkpoint-enabled run."""
    policy = getattr(agent, "continuation_policy", ContinuationPolicy())
    exhausted = api_call_count >= agent.max_iterations or agent.iteration_budget.remaining <= 0
    if (policy.enabled is not True or not exhausted or interrupted or failed
            or exit_reason not in {"unknown", "budget_exhausted"}
            or getattr(agent, "_persist_disabled", False)
            or not getattr(agent, "_session_db", None) or getattr(agent, "_continuation_ready", False)):
        return None
    # A normal final answer exactly on the cap is completed work, not a restart trigger.
    if final_response is not None:
        return None
    return finish_handoff(agent, messages, None, "iteration_cap")


def seed_unstarted_continuation(agent, user_message, conversation_history):
    """Recover an admitted-but-unstarted child only at its FIRST user-turn boundary."""
    db = getattr(agent, "_session_db", None)
    if db is None or conversation_history or getattr(agent, "_persist_disabled", False):
        return user_message
    state = read_checkpoint(db, agent.session_id)
    seed = state.get("seed")
    if not seed or db.get_messages(agent.session_id, limit=1):
        return user_message
    # Cron wraps its prompt in delivery guidance; that prompt already carries the seed.
    if isinstance(user_message, str) and seed in user_message:
        return user_message
    prefix = f"Saved context for this new session:\n{seed}\n\nCurrent user request (takes precedence):\n"
    if isinstance(user_message, list):
        return [{"type": "text", "text": prefix}, *user_message]
    return prefix + str(user_message)
