"""Fresh-session continuation at the gateway turn boundary, under the source scope."""
from __future__ import annotations

import asyncio

from agent.continuation import load_policy
from hermes_state_continuation import claim_continuation


async def _notice(runner, source, text):
    adapter = runner._adapter_for_source(source)
    if adapter is None:
        raise RuntimeError("Continuation needs an available origin adapter")
    metadata = dict(runner._thread_metadata_for_source(source) or {})
    metadata["_interim_send"] = True
    result = await adapter.send(source.chat_id, text, metadata=metadata)
    if getattr(result, "success", True) is False:
        raise RuntimeError("Continuation notice delivery failed")


async def run_with_continuations(runner, message, context_prompt, history, source, session_id, **kwargs):
    """Keep one gateway run owner while rotating the agent and transcript per handoff."""
    policy = await asyncio.to_thread(load_policy)
    while True:
        result = await runner._run_agent_inner(message, context_prompt, history, source, session_id, **kwargs)
        if (not policy.enabled or not result.get("continuation_ready")
                or result.get("interrupted") or result.get("failed")):
            return result
        key, generation = kwargs.get("session_key"), kwargs.get("run_generation")
        if not key or (generation is not None and not runner._is_session_run_current(key, generation)):
            return result
        parent_id = result.get("session_id") or session_id
        db = await asyncio.to_thread(runner.session_store._db_for_key, key)
        if db is None:
            return result
        claim = await asyncio.to_thread(claim_continuation, db, parent_id, policy.max_per_origin)
        if claim.status == "limit":
            await _notice(runner, source,
                          f"Continuation limit reached ({policy.max_per_origin}). Owner action needed: "
                          f"review the handoff with /resume {parent_id}, then ask to continue.")
            return result
        if claim.status != "spawn":
            return result
        entry = await runner.async_session_store.switch_session(
            key, claim.session_id, expected_session_id=parent_id)
        if entry is None:
            return result  # /new or /resume won the race; never override the user's route.
        if generation is not None:
            if not runner._rebind_turn_lease(key, generation, claim.session_id):
                return result
        await asyncio.to_thread(
            runner._sync_telegram_topic_binding, source, entry, reason="checkpoint-continuation")
        await _notice(runner, source,
                      f"Session will continue as {claim.session_id}. "
                      f"To resume later: /resume {claim.session_id}, then ask to continue.")
        if generation is not None and not runner._is_session_run_current(key, generation):
            return result
        session_id, message, history = claim.session_id, claim.seed, []
        # The new seed owns its own user row, never the original event's platform id
        # or display override. Keep source, channel prompt, tools/model routing and lease.
        kwargs = {k: v for k, v in kwargs.items() if k in {
            "session_key", "run_generation", "channel_prompt", "moa_config",
        }}
