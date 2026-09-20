"""Checkpoint cron progress and schedule a one-shot only after the old worker exits."""
from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

from agent.continuation import load_policy, progress_handoff
from hermes_state_continuation import claim_continuation, save_checkpoint, read_checkpoint

# Preserve execution/delivery policy; never replay the data-collection script or a
# context_from dependency already included in the original objective.
_JOB_FIELDS = (
    "skills", "skill", "model", "provider", "base_url", "provider_snapshot", "model_snapshot",
    "deliver", "failure_deliver", "origin", "enabled_toolsets", "workdir", "reasoning_effort",
    "attach_to_session",
)


def enqueue_continuation(job, claim):
    from cron.jobs import _jobs_lock, load_jobs, _save_jobs_unlocked, parse_schedule
    from hermes_time import now
    start = (now() + timedelta(seconds=1)).isoformat()
    child = {k: copy.deepcopy(job[k]) for k in _JOB_FIELDS if k in job}
    child.update(
        id=claim.session_id, name=claim.session_id, prompt=claim.seed,
        continuation_session_id=claim.session_id, schedule=parse_schedule(start),
        schedule_display=start, repeat={"times": 1, "completed": 0},
        enabled=True, state="scheduled", created_at=now().isoformat(), next_run_at=start,
        last_run_at=None, last_status=None, last_error=None, failure_streak=0,
        no_agent=False, script=None, context_from=None,
    )
    with _jobs_lock():
        jobs = load_jobs()
        existing = next((j for j in jobs if j["id"] == child["id"]), None)
        if existing is not None:
            return existing
        _save_jobs_unlocked(jobs + [child])
    from cron.scheduler_provider import resolve_cron_scheduler
    from cron.scheduler import CronSchedulerRegistrationError
    try:
        resolve_cron_scheduler().register_job(child)
    except Exception as exc:
        raise CronSchedulerRegistrationError(child, exc) from exc
    return child


def continue_cron_job(job, agent, prompt, *, idle=False, future=None):
    """Called before run_job releases its session DB; callback owns a separate DB handle."""
    policy = load_policy()
    db = getattr(agent, "_session_db", None)
    if not policy.enabled or db is None:
        return None
    sid = agent.session_id
    if idle:
        # Read committed progress: the timed-out worker may still be mutating its live list.
        messages = db.get_messages_as_conversation(sid)
        save_checkpoint(db, sid, progress_handoff(agent, messages, objective=prompt), "cron_idle_timeout")
    if not read_checkpoint(db, sid).get("handoff"):
        return None
    claim = claim_continuation(db, sid, policy.max_per_origin)
    if claim.status == "limit":
        return (f"Continuation limit reached ({policy.max_per_origin}). Owner action needed: "
                f"review /resume {sid} in this profile's Hermes CLI and ask to continue.")
    if claim.status != "spawn":
        return None
    home = Path(db.db_path).parent
    saved_job = copy.deepcopy(job)

    def schedule(_future=None):
        # A Future callback has no guaranteed ContextVars. Rebind ALL profile state,
        # including the cron store, before reading policy or writing jobs.json.
        from gateway.run import _profile_runtime_scope
        from cron.jobs import use_cron_store
        with _profile_runtime_scope(home), use_cron_store(home):
            enqueue_continuation(saved_job, claim)

    if future is not None and not future.done():
        future.add_done_callback(schedule)
        return (f"Checkpoint saved. Session will continue as {claim.session_id} after the idle worker exits. "
                f"Resume later in this profile's Hermes CLI with /resume {claim.session_id} and ask to continue.")
    schedule()
    return (f"Session will continue as {claim.session_id}. "
            f"Resume later in this profile's Hermes CLI with /resume {claim.session_id} and ask to continue.")


def cron_session_id(job, default):
    """Only internally reserved cron sessions may bypass normal session-id generation."""
    sid = job.get("continuation_session_id")
    if not sid:
        return default
    if not load_policy().enabled:
        raise ValueError("Automatic continuation is disabled")
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        state = read_checkpoint(db, sid)
        if not state.get("root") or state.get("seed") != job.get("prompt"):
            raise ValueError("Invalid cron continuation reservation")
    finally:
        db.close()
    return sid
