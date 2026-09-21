# Checkpoint and continue

Implemented C1, C2, and C3. Automatic continuation is opt-in. No core tools, environment configuration, schema migration, or compatibility aliases were added.

## Design

- **C1:** The agent requests wrap-up exactly once per run at the soft iteration threshold. The instruction is appended to a newly produced, not-yet-persisted tool result, before its first model submission. Historical messages, system prompt, and tool schema remain unchanged. A deterministic checkpoint is saved immediately; the final model summary replaces its summary field. If the model keeps calling tools until the hard cap, the deterministic checkpoint is finalized without an extra model call. A normal final answer exactly at the cap is not treated as unfinished work. Interruptions and unrelated failures do not initiate budget continuation.
- **Storage:** Handoffs and lineage live in the existing session `model_config._continuation` field. A SQLite transaction creates the child, records the parent's sole child, and increments the root chain count. Independent database handles racing the same completion can admit only one child. Child IDs and titles are `<root-session-id>-cont-N`; origin and working-directory metadata are retained, while the transcript starts fresh with the saved handoff as its first user seed.
- **C2 gateway:** The existing profile-scoped turn wrapper starts each admitted child with empty history and the handoff. It retains source/chat/topic routing, switches the session using the expected parent ID, rebinds the held turn lease, and updates Telegram topic binding. The originating adapter receives the continuation/resume notice or the cap escalation. Generation checks avoid continuing a displaced turn.
- **C2 cron:** A continuation is an existing-format, one-shot agent job registered with the configured scheduler provider. It retains execution and delivery settings. The reserved session ID is validated against its saved seed before execution; disabling continuation prevents pending continuation jobs from auto-running.
- **C3:** The real inactivity watchdog marks its timeout. Before teardown, cron saves the original objective, committed recent progress, existing todo markers, and observed file paths. A late worker's Future callback schedules the continuation only after that worker exits. The callback explicitly binds the originating profile and cron store; detached teardown also retains its captured scope.
- **Recovery:** Manually resuming a reserved child that never started loads the saved seed at its first user-turn boundary. A current user instruction takes precedence. Subsequent turns do not replay the seed.

The cron executor was mechanically extracted into `cron/scheduler_run.py` in a separate refactor commit before adding behavior. Existing late-import patch seams are retained; the public scheduler entry point and documentation point to the extracted implementation.

## Configuration

In the owning profile's `config.yaml`:

```yaml
continuation:
  enabled: true                # default false: opt in to additional model runs
  max_per_origin: 3            # nonnegative integer; 0 checkpoints but admits no child
  soft_budget_fraction: 0.9    # finite number strictly between 0 and 1
```

The cap is per root session's continuation chain, not a lifetime quota for every future message in a chat or every occurrence of a recurring cron job. Policy is loaded at run boundaries. Extremely small iteration budgets use the last available pre-cap tool boundary; a one-iteration run uses the hard-cap checkpoint fallback.

## Files touched

| Area | Files |
| --- | --- |
| Agent policy and checkpoints | `agent/continuation.py`, `agent/agent_init.py`, `agent/conversation_loop.py`, `agent/tool_executor.py`, `agent/turn_context.py`, `agent/turn_final_response.py`, `agent/turn_finalizer.py` |
| Durable admission | `hermes_state_continuation.py` |
| Gateway orchestration | `gateway/run_continuation.py`, `gateway/run_turn.py`, `gateway/run_turn_runner.py` |
| Cron orchestration and lifecycle | `cron/continuation.py`, `cron/scheduler.py`, `cron/scheduler_run.py`, `cron/scheduler_detached_worker.py` |
| Config and documentation | `hermes_cli/config_defaults.py`, `cron/AGENTS.md`, `website/docs/developer-guide/cron-internals.md`, this file |
| Behavioral tests | `tests/agent/test_checkpoint_continue.py`, `tests/hermes_state/test_continuation.py`, `tests/gateway/test_checkpoint_continue_e2e.py`, `tests/cron/test_checkpoint_continue.py` |

## Test evidence

All tests ran through the canonical runner, with temporary homes and scratch files redirected inside this worktree. The worktree-local virtual environment uses already-installed, read-only dependency directories; no packages were installed globally. Inference and transport are scripted, while agent turns, tool execution, SQLite, session routing, watchdog handling, cron job execution, and profile scopes use real imports and runtime paths.

Final command:

```sh
PATH="/Library/Developer/CommandLineTools/usr/bin:$PATH" TEMP="$PWD/.test-tmp" TMP="$PWD/.test-tmp" scripts/run_tests.sh \
  tests/agent/test_checkpoint_continue.py tests/hermes_state/test_continuation.py \
  tests/gateway/test_checkpoint_continue_e2e.py tests/cron/test_checkpoint_continue.py \
  tests/agent/test_iteration_budget_warning.py tests/agent/test_turn_finalizer_iteration_limit_exit.py \
  tests/agent/test_verification_continuation_budget.py tests/agent/test_turn_iteration_prep.py \
  tests/cron/test_cron_inactivity_timeout.py tests/cron/test_sessiondb_init_hang.py \
  tests/cron/test_cleanup_timeout.py tests/cron/test_scheduler_cron_session_isolation.py \
  --file-timeout 90 --file-retries 0 -q
```

Actual output (`.test-tmp/final-regression-tests.log`):

```text
=== Summary: 12 files, 68 tests passed, 0 failed (100% complete) in 29.3s (20 workers) ===
```

Coverage includes soft/hard crossing → persisted handoff → new seeded session → cap enforcement; independent-handle admission races; exact-once wrap-up and unchanged submitted prefixes/tool schemas; manual recovery; same-topic routing; real cron watchdog and reserved-child execution; and A→B→A under multiplex for both gateway execution and delayed cron scheduling. The cron profile test completes the old Future from a thread without inherited scope and checks the owning store and scheduler registration.

Red-on-base check: temporarily restored original integration files from base `732d8a28ca`, retained the new helper modules to avoid mere missing-import failures, ran the agent/gateway/cron feature files through `scripts/run_tests.sh`, and restored the implementation in `finally`.

Actual output (`.test-tmp/red-on-base.log`):

```text
=== Summary: 3 files, 5 tests passed, 7 failed (100% complete) in 21.0s (20 workers) ===
=== 3 files with test failures (7 tests failed) ===
  tests/gateway/test_checkpoint_continue_e2e.py  (1 test failed)
  tests/agent/test_checkpoint_continue.py  (3 tests failed)
  tests/cron/test_checkpoint_continue.py  (3 tests failed)
```

The passing cases in that run exercise configuration helpers that were intentionally retained. The failing cases exercise the actual missing integrations. `scripts/check_compat_pointers.py` and Git whitespace checks also completed with exit 0 and no output. The full repository suite and live provider/chat acceptance were not run.

## Limitations and recovery boundaries

- Automatic dispatch covers Hermes-managed messaging gateway and cron loops. CLI/TUI sessions receive a saved handoff and manual resume instructions. Externally owned/proxy agent loops are not instrumented.
- Admission is at-most-once, not an automatic crash-recovery dispatcher. A process crash after reservation but before dispatch can leave an unstarted child. Its seed is durable and manual `/resume <child-id>` recovers it; no automatic retry scanner was added.
- A permanently wedged cron worker prevents automatic continuation from starting. This avoids concurrent workers modifying the same workspace. Existing process termination behavior is unchanged.
- File paths and todo markers are observations, not a filesystem snapshot or verified Git status. The model is asked to inspect uncommitted work; the deterministic fallback explicitly marks inventory as requiring verification. Ephemeral remote workspace contents are not exported. Durable working directories are necessary for file recovery.
- Cron escalation uses existing delivery settings. Jobs with local-only output or no owner delivery route cannot produce a remote owner notification. Cron session manual resume uses the owning profile's Hermes CLI.
- Delayed scheduler registration errors can leave the durable one-shot job requiring existing scheduler recovery/registration procedures. No new dispatcher or retry service was introduced.
- No C3 placeholder or deferred implementation remains. Potential later work is a durable dispatch-recovery reconciler and explicit remote-artifact preservation, with idempotency and ownership tests.

## Commit delivery and sandbox constraint

The original worktree's `.git` points to shared metadata outside this worktree. Attempting normal staging failed with an index-lock permission error. The sandbox forbids changing those external files and cannot grant escalation. Therefore the original shared branch/index could not be advanced.

The commits instead live in worktree-local `.checkpoint-git`, on a branch named `feat/checkpoint-continue`, using the original object database read-only. The first commit is `dd4d41858e` (`refactor(cron): extract agent job execution from scheduler`); the feature commit follows it. `checkpoint-continue.bundle` delivers both commits with the original base as prerequisite. Source edits remain in this worktree. The preexisting untracked `CODEX-TASK.md` was not staged or changed.

From an environment authorized to write the original shared Git metadata, adopt the bundle after reviewing the fetched tree:

```sh
git fetch ./checkpoint-continue.bundle feat/checkpoint-continue
git diff FETCH_HEAD
git reset --mixed FETCH_HEAD
```

The mixed reset advances the checked-out branch/index without replacing working files. Review any intervening edits before adoption. Local evidence and metadata directories are not part of either commit.

## PR body draft

**Title:** feat(agent): checkpoint and continue bounded long-running sessions

Long Hermes-managed runs can exhaust iteration limits or hit cron inactivity timeouts with no resumable progress record. Opt-in continuation now saves a handoff at a soft budget boundary, preserves a deterministic checkpoint at the hard backstop, and starts a fresh session with that context through the existing gateway or cron orchestration. Each root chain has a transactional continuation cap and reports when owner action is required.

Cron watchdog checkpoints include the original objective, existing step markers, recent committed progress, and observed file paths. Continuations retain origin/profile routing and wait for the old worker to exit. A reserved child can also be resumed manually after a dispatch crash. The cron executor extraction is a separate preceding commit.

Validation: 68 tests pass across 12 focused and regression files, with retries disabled. Seven integration assertions fail with the original integration code restored. Coverage includes real agent/tool/SQLite paths, seeded continuation and cap enforcement, gateway topic routing, cron watchdog/child execution, and A→B→A multiplex profile isolation including delayed callbacks. No full-suite or live-network acceptance claim. Automatic crash redispatch and filesystem snapshots are outside this change; continuation defaults off.
