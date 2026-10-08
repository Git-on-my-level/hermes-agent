# goal-heartbeat

A model-level heartbeat for gateway sessions with an active `/goal` that went quiet.

In the gateway a parked goal resumes only when something starts a turn, normally its waiter's
exit notice. If the waiter hangs, dies without a notice (e.g. a gateway restart), or waits on
the wrong thing, the session stays silent indefinitely: the goal barrier's 30-minute cap is
applied lazily, on a turn that never comes. A liveness probe cannot catch "alive but waiting on
the wrong thing", so this plugin wakes the model instead.

Every `interval_minutes` without any message in such a session, it injects a check-in turn:
verify the wait is still progressing and still the right wait; act if not; otherwise ensure a
waker is armed and reply exactly `[SILENT]`. A silent internal turn leaves the goal untouched
(`GatewayRunner._silent_internal_turn`: no judge call, no turn spent, no status line, no
continuation). Heartbeats since the last real event (a user message or process notice; not a
heartbeat or goal continuation) are counted from the session history; check number `escalate_after` asks the agent to message the user with what is
stuck, and heartbeats stop until something new happens.

```yaml
plugins:
  enabled: [goal-heartbeat]
  entries:
    goal-heartbeat:
      allow_gateway_injection: true   # required
      interval_minutes: 50            # default 50, min 15
      escalate_after: 3               # default 3
      enabled: true                   # false pauses new heartbeats (re-read every minute)
prompt_caching:
  cache_ttl: auto                     # 1h for chat sessions, so a 50-minute heartbeat hits a warm cache
```

Dry run against a home's live database: `python3 plugins/goal-heartbeat/__init__.py --dry-run [--interval M] [--escalate N]`.

Scope: each profile load watches its own `state.db` (sessions keyed `agent:*`). A dispatch that never
shows up in the session is retried after 10 minutes. Removing the plugin from `plugins.enabled`
stops its thread on the next tick.
