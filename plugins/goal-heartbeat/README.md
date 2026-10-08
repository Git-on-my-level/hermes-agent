# goal-heartbeat

A model-level heartbeat for gateway sessions with an active `/goal` that went quiet.

In the gateway a parked goal resumes only when something starts a turn, normally its waiter's
exit notice. If the waiter hangs, dies without a notice (e.g. a gateway restart), or waits on
the wrong thing, the session stays silent indefinitely: the goal barrier's 30-minute cap is
applied lazily, on a turn that never comes. A liveness probe cannot catch "alive but waiting on
the wrong thing", so this plugin wakes the model instead.

Every `interval_minutes` without any message in such a session, it injects a check-in turn:
verify the wait is still progressing and still the right wait; act if not; otherwise ensure a
waker is armed and reply `[SILENT]` (suppressed on internal turns). Heartbeats since the last
real event (a user message, a process notice, any non-heartbeat turn) are counted from the
session history; check number `escalate_after` asks the agent to message the user with what is
stuck, and heartbeats stop until something new happens.

```yaml
plugins:
  enabled: [goal-heartbeat]
  entries:
    goal-heartbeat:
      allow_gateway_injection: true   # required
      interval_minutes: 50            # default 50, min 5
      escalate_after: 3               # default 3
      enabled: true                   # false pauses new heartbeats (re-read every minute)
prompt_caching:
  cache_ttl: auto                     # 1h for chat sessions, so a 50-minute heartbeat hits a warm cache
```

Dry run against a home's live database: `python3 plugins/goal-heartbeat/__init__.py --dry-run [--interval M] [--escalate N]`.

Scope: sessions in the gateway's own `$HERMES_HOME/state.db`, keyed `agent:*`.
