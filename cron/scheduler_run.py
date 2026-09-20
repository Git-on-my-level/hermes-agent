"""Execution of one agent-backed cron fire; scheduler.py keeps the public entry point."""
from __future__ import annotations

from typing import Optional


def run_job(
    job: dict, *, defer_agent_teardown: Optional[list] = None, extra_prompt: Optional[str] = None,
    cancel_event: Optional[_CancelEventLike] = None, execution_id: Optional[str] = None,
) -> tuple[bool, str, str, Optional[str]]:
    """Execute a single cron job. Returns (success, full_output_doc, final_response, error).
    ``defer_agent_teardown``: if a list, the live agent is appended instead of torn down; the caller
    MUST call ``_teardown_cron_agent(agent)`` AFTER delivery (a torn-down async client can't
    deliver). ``extra_prompt``: per-fire context, never persisted.

    ``defer_agent_teardown``: when a caller passes a list, ``run_job`` skips the agent's async-resource
    teardown (``agent.close()`` + ``cleanup_stale_async_clients()``) in its ``finally`` block and instead
    appends the live agent to that list. The caller is then responsible for calling
    ``_teardown_cron_agent(agent)`` AFTER it has delivered the result. This closes the ordering window in
    #58720 where delivery ran against a torn-down async client (defense-in-depth alongside the
    interpreter-shutdown guard). When ``None`` (the default) teardown happens inline as before, so every
    existing caller is unchanged.
    ``extra_prompt``: optional per-run context from ``cronjob(action='run', prompt=...)`` (#57331). Appended
    to the stored prompt for this fire only — never persisted to the job definition.
    """
    from cron.scheduler import (
        _prepare_job_prompt, _hermes_now, logger, _CronRunScope,
        _reload_dotenv_and_publish_delivery_target, _load_cron_job_config,
        _resolve_cron_agent_setup, _open_cron_session_db, _construct_cron_agent,
        _FireAudit, _run_agent_with_watchdog, _final_response_from_result,
        _is_cron_silence_response, _run_doc_header, _finalize_cron_session,
        _teardown_cron_agent,
    )
    job_id = job["id"]
    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")

    early, prompt = _prepare_job_prompt(job, job_id, job_name, extra_prompt, cancel_event)
    if early is not None:
        return early
    from run_agent import AIAgent

    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"
    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])

    agent = None
    model = ""
    _session_db = None
    _audit: Optional[_FireAudit] = None
    _worker_state: dict = {}
    scope = _CronRunScope(job, job_id, execution_id)
    try:
        scope.enter()
        if scope.workdir:
            logger.info("Job '%s': using task-scoped workdir %s", job_id, scope.workdir)
        _reload_dotenv_and_publish_delivery_target(job)

        jc = _load_cron_job_config(job, job_id, job_name)
        _cfg = jc.cfg
        model = jc.model
        setup = _resolve_cron_agent_setup(job, job_id, job_name, jc)
        if setup.blocked is not None:
            return setup.blocked
        model = setup.model

        # Open state.db only after every early-return gate has passed.
        _session_db = _open_cron_session_db(job)
        agent = _construct_cron_agent(
            AIAgent, job, _cfg, setup, workdir=scope.workdir, session_id=_cron_session_id,
            session_db=_session_db)
        _audit = _FireAudit(job, job_id, model)

        result = _run_agent_with_watchdog(
            agent, prompt, job, job_id, job_name, scope.task_id, cancel_event,
            worker_state=_worker_state)
        final_response = _final_response_from_result(result, job_id, job_name, AIAgent)
        # Keep final_response clean for delivery logic (empty = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        output = _run_doc_header(job, job_name, job_id, prompt) + f"## Response\n\n{logged_response}\n"
        logger.info("Job '%s' completed successfully", job_name)
        _audit.write(dict(result, response_silent=_is_cron_silence_response(final_response or "")), None)
        return True, output, final_response, None

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        # Cowork-style unreachable-model re-run (cron/unreachable_retry.py): flag failures where
        # the model was never reached (transient network/DNS, zero API calls) so the bookkeeping
        # tail can schedule a bounded automatic re-run instead of waiting a full period.
        try:
            from cron.unreachable_retry import is_model_unreachable_failure
            if is_model_unreachable_failure(e, agent):
                job["_model_unreachable"] = True
        except Exception:  # classification must never mask the real failure
            logger.debug("Job '%s': unreachable-failure classification failed", job_id)
        # No audit row when we failed before the agent existed; the audit write must never raise.
        if _audit is not None:
            _audit.write({}, error_msg)
        from cron.scheduler_diagnostics import format_run_error
        output = (
            _run_doc_header(job, f"{job_name} (FAILED)", job_id, prompt)
            + format_run_error(e)
        )
        return False, output, "", error_msg

    finally:
        from cron.scheduler_detached_worker import defer_teardown_to_running_worker
        _worker_teardown_deferred = defer_teardown_to_running_worker(
            _worker_state.get("future"), _session_db, agent, job_id, job_name, _cron_session_id)
        scope.exit()
        if _session_db and not _worker_teardown_deferred:
            _finalize_cron_session(_session_db, agent, job_id, job_name, _cron_session_id)
        # Tear down the ephemeral agent or the gateway leaks fds per tick (EMFILE). With deferred
        # teardown, hand the live agent back: delivery needs a live async client.
        # Release subprocesses, terminal sandboxes, browser daemons, and the main OpenAI/httpx client held
        # by this ephemeral cron agent. Without this, a gateway that ticks cron every N minutes leaks fds
        # per job until it hits EMFILE (#10200 / "too many open files"). When the caller opted to defer
        # teardown (passed a list), hand the live agent back instead of closing it here — delivery must run
        # against a live async client, and the caller tears down afterwards (#58720).
        if not _worker_teardown_deferred:
            if defer_agent_teardown is not None:
                if agent is not None:
                    defer_agent_teardown.append(agent)
            else:
                _teardown_cron_agent(agent, job_id)

