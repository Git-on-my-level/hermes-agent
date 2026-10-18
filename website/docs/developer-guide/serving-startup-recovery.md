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

On first deploy, a managed install without a serving record can bootstrap one only
when HEAD equals its clean completed install stamp, no update/completion marker
exists, and PM's committed dependency stamp matches the checkout. It publishes
and immediately execs the immutable pair. It never adopts unknown update state.
An unfinished or unknown checkout fails at the actual bootstrap process boundary:
one clear stderr line, `startup_failed` with a fatal needs-attention startup entry
through the existing status/alert path, a 60-second retry floor, then exit 78.
The floor applies even to old supervisors without launcher markers; the startup
watchdog is disarmed for that intentional wait. Developer and packaged installs
keep their ownership contracts. Serving never finishes an update itself.

The published snapshot carries a **venv seam**. POSIX script jobs (cron `no_agent`)
resolve their interpreter against their own source tree, which for a snapshot is
`serving-source/<commit>/venv/bin/python` — a path publication never populates, so
every script tick failed with `dependency environment interpreter is missing` until
manual repair. Publication now binds that path to the committed generation's venv
(relative symlink) once dependency declarations are proven equal; a real tree at
the path is never replaced, and Windows is untouched (its jobs resolve the
committed venv directly). As last resort the scheduler falls back to the recorded
generation when the seam is missing; `HERMES_SNAPSHOT_VENV_FALLBACK=0` restores
the strict missing-interpreter error. Snapshots published before this change gain
the seam on their next successful update publication.

Update completion treats a rejected generation publication as failure: it exits
non-zero with the publication reason and does not advise restarting gateways.
Bootstrap receipts are refreshed only after successful serving publication.

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
Synchronous metadata/config/status readers wait for deferred registration up to
that threshold outside the discovery lock, so YAML/auth bridges and platform rows
are complete when their lookup returns. Gateway transport startup explicitly
selects background loading; other adapters can proceed while a slow worker remains
pending. Repeated lookups reuse that worker instead of starting duplicate imports.
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

## First deployment on m5 after review and merge

#83 is already merged into fork `prod`. After #84 is reviewed and merged, keep the current gateway running while
updating the original maintained checkout, not a scratch worktree or a serving
archive. On m5 that checkout is `~/.hermes/hermes-agent`. Use the reviewed merge
SHA on fork `prod`; stop if the update returns non-zero:

```sh
cd "$HOME/.hermes/hermes-agent"
./hermes update --remote fork --branch prod --sha <reviewed-merged-sha> --no-gateway-restart
```

Successful completion logs `hermes: completed serving generation published:`
with the record path and commit. Before restarting, validate that record using a
stdlib Python from the maintained checkout (this probe does not install anything):

```sh
python3 - <<'PY'
from pathlib import Path
import subprocess
from hermes_cli.serving_generation import read_generation, record_path
from pm.environments import venv_python
root = Path.cwd().resolve()
record = read_generation(root)
assert record is not None, 'No validated serving generation; do not restart'
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, encoding='utf-8').strip()
assert record['commit'] == head, (record['commit'], head)
print('VALID:', record_path(root))
print('commit:', record['commit'])
print('source:', record['source'])
print('python:', venv_python(Path(record['generation']) / 'venv'))
PY
```

`VALID:` must name the owning install's
`~/.hermes/installs/<install-key>/serving-generation.json`; its commit must equal
the reviewed merged SHA. Validation includes source stamp, dependency declarations,
interpreter, and skills/locales/scripts. Only after update and validation succeed:

```sh
./hermes gateway restart
```

Expect `hermes: serving completed source/dependency generation; unfinished updates
remain owed to` in gateway stderr (`~/.hermes/logs/gateway.error.log`). A consistent
legacy install that had no record and reaches first boot instead also logs
`hermes: first-deploy serving generation published from consistent install`.
An inconsistent install emits `hermes: Refusing gateway startup:` followed by
its reason and `retry backoff 60s; exit 78`; repair from the original install with
`hermes update`, then repeat record validation before retrying restart.

After restart verify the running SHA, readiness for every configured platform,
`getUpdates` progress and replay admission diagnostics. The local 120-second plugin
stopgap can remain as a warning threshold for critical platform loading. No install
or live gateway restart is performed by development tests.
