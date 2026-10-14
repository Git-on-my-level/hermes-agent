# Serving startup and unfinished updates

Gateway startup must not wait for dependency provisioning or an update completion
worker. Successful source completion publishes `serving-generation.json` in the
install's PM state. It pairs the verified dependency generation with an immutable
source snapshot under `serving-source/<commit>`. Source is captured at completion,
not inferred from PM's dependency-build snapshot: code can change without changing
dependencies. It reuses the distribution archive contract and retains runtime
bridge scripts, locales, catalog manifests and bundled skills. Boot selects that pair before PM probing or update-lock acquisition.
Both directories are leased for the process lifetime; GC keeps the selected pair
and collects older unleased snapshots after the existing age floor.

Until the first completed update publishes a pair, a legacy serving launch skips
update work and uses its committed interpreter with a warning. It cannot provide
an immutable old-code guarantee. Install this change through a completed `hermes
update` before relying on recovery. Developer and packaged installs keep their
existing ownership contracts. Repairs run through the original install's CLI,
not the immutable gateway snapshot. A pending completion remains owed to an
explicit update or the next idle converge window. Failed/partial converge receipts
produce ERROR and stderr alerts even on subsequent busy skips; nonzero update
exits alert and clear the planned drain.

Optional plugin loads default to a 120-second deadline. A platform plugin uses
that value as an ERROR warning threshold, then keeps waiting for registration;
it is never abandoned for being slow. Gateway discovery and deferred platform imports
run off the serving loop; adapter factories retain loop affinity. Missing configured adapters also log ERROR and retain
the existing fatal/retrying, needs-attention status. Startup git identity probes allow 120 seconds; failure to identify a
completed source tree leaves completion owed. Network timeouts are unchanged.
A completed PM worker result no longer fails because interpreter shutdown takes
more than five seconds; exceptional cleanup escalates terminate to kill.

Telegram preserves pending updates on cold boot, webhook startup, reconnects and
conflict retries. `drop_pending_on_cold_boot=true` is deprecated, warns, and preserves
input too. Existing bot-scoped durable update-ID admission suppresses completed
replays, while unfinished dispatches remain recoverable. No extra age filter silently
removes input; Telegram's server-side retention remains a recovery limit.

## Coordinator rollout after review

Merge #83 first, then merge the separate recovery PR into fork `prod`. Run from
the original maintained install:

```sh
hermes update --remote fork --branch prod --sha <reviewed-merged-sha> --no-gateway-restart
hermes gateway restart
```

Before restart, check that the update completed and published the serving record.
After restart check the running code SHA, readiness for every configured platform,
`getUpdates` progress and replay admission diagnostics. The local 120-second plugin
stopgap can remain; it becomes a warning threshold for critical platform loading.
No installation or live gateway restart is performed by this PR's development tests.
