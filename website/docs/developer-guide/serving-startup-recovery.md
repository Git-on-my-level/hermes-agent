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

Serving validates the source stamp, dependency declarations, and skills/locales/scripts
before selecting a pair. The bootstrap fences core imports to that source tree,
including modules preloaded by editable finders during Python site processing.
Thus `__file__`, package resources and `install_root()` agree. Unknown/failed Git
status is treated as dirty and cannot publish a clean completed pair.

Until a completed update publishes a validated pair, managed serving refuses to
boot with a loud error instead of trusting a mutable checkout or adopting an
unfinished dependency build. Complete `hermes update` from the original owning
install before restart. Developer and packaged installs keep their ownership
contracts. Serving never acquires the update lock or finishes the update itself.

Failed/partial converge receipts alert once per receipt ID, including busy skips.
An owed completion on an already-current checkout reserves its retry before drain,
backs off for at least one hour (doubling on each attempt), and stops automatic
completion repair after three attempts. A marker that remains set alerts once for
that obligation. Clearing the marker resets the retry budget; changing the target
starts a new obligation. Explicit `hermes update` remains the operator repair path.

Optional plugin loads default to a 120-second deadline. Critical platform loads
return a live pending registration after at most 100ms of waiting, leaving their
import/register worker and a daemon completion observer outside the discovery lock.
The configured threshold still emits ERROR if registration has not completed.
Other platforms materialize independently, late registration becomes eligible for
the existing reconnect watcher, and unloading a pending context cancels its late
registrations. Missing configured adapters retain needs-attention status. Gateway
discovery runs off the serving loop; adapter factories retain loop affinity.
Startup Git identity probes allow 120 seconds. Network timeouts are unchanged.
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
