# session-inject

Resume or nudge an existing gateway session from a shell, cron job, or another agent:

```bash
hermes inject <session_id | session_key> "message"
hermes inject --list-stalled [--hours 6]   # recent gateway sessions and who spoke last
```

`hermes inject` writes one JSON request into `$HERMES_HOME/state/inject-spool/` (mode 700).
Inside the gateway process a daemon thread drains the spool every 5 s through the supported
plugin API (`ctx.inject_message`), so the message runs as a user turn in that session.

Status by directory: `sent/` = dispatched to the gateway; `done/` = seen persisted as a user turn
in that session; `failed/` = refused for ~10 min, or dispatched but never seen within 30 min (an
unknown route or failed authorization is only logged by the gateway). A claim left by a crash
(`*.inflight`) returns to the queue after a minute. `hermes -p <profile> inject` uses that
profile's spool, drained by that profile's load of the plugin.

Enable (host `config.yaml`):

```yaml
plugins:
  enabled: [session-inject]
  entries:
    session-inject:
      allow_gateway_injection: true
```

Injected turns are internal events: they queue behind a running turn and do not carry gateway
control, so `/stop` must still come from the chat itself. Anyone who can write the spool
directory can start turns; keep it private.
