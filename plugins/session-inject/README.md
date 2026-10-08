# session-inject

Resume or nudge an existing gateway session from a shell, cron job, or another agent:

```bash
hermes inject <session_id | session_key> "message"
hermes inject --list-stalled [--hours 6]   # recent gateway sessions and who spoke last
```

`hermes inject` writes one JSON request into `$HERMES_HOME/state/inject-spool/` (mode 700).
Inside the gateway process a daemon thread drains the spool every 5 s through the supported
plugin API (`ctx.inject_message`), so the message runs as a user turn in that session. Accepted
requests move to `done/`; requests the gateway keeps refusing move to `failed/` after ~10 min.

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
