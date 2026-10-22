# Telegram loop isolation

Telegram HTTP requests and PTB's `Updater` polling run on one dedicated
`telegram-io` thread and asyncio loop. Application handlers and agent turns stay
on the gateway loop. Polling acceptance commits each update to a profile- and
bot-scoped SQLite inbox (`telegram_update_inbox_<bot-id>.sqlite3`, private file
permissions, FULL synchronous commits) before PTB advances its offset. The
bounded 256-item gateway queue is a cache; its suspended pump holds at most one
256-row page. Unfinished inbox rows have no expiration and replay on replacement
or restart. Completed dispatch writes its replay receipt before retiring the
inbox row; replay of an already-completed receipt retires it without dispatch.
Disk errors fail acceptance rather than acknowledge unpersisted input. The database
and rollback/WAL/SHM sidecars are created under umask 0077 and tightened to 0600
on each transaction, including existing stores.

`platforms.telegram.extra.inbox_max_updates` defaults to 10,000 rows;
`inbox_max_age_seconds` defaults to 21,600 seconds (six hours). Either limit
pauses new acceptance without deleting unfinished rows. Duplicate prefixes of
partially accepted batches remain safe to accept. PTB cannot advance the batch
offset until every item is durable; unaccepted tails remain on Telegram (whose
retention is about 24 hours, so operators must resolve a stuck consumer promptly).
Completion wakes acceptance without periodic disk polling.

`platforms.telegram.extra.backpressure_stall_seconds` defaults to 300 seconds.
No completed input during backpressure for this interval logs ERROR and publishes
`needs_attention` with `telegram_consumer_stalled` through runtime status, at most
once per five minutes. This alert does not rebuild the adapter or discard input.
Dispatch entry and enqueue activity do not count as consumer completion.

Queue pressure and outstanding acceptance commits pause polling/ingress stall
clocks. Stop cancels the polling action and delivery pump without waiting for a
saturated dispatcher. Accepted input remains on disk; partial batch tails that
were not accepted keep their previous Telegram offset and remain unacknowledged.
The pump wakes on committed input, rather than querying the disk on an idle timer.
The SDK retains its update-processor
concurrency policy. HTTPX returns fully read response bytes across the request
boundary. Error and progress callbacks re-enter the gateway loop, with conflict
retry disarmed synchronously on the polling loop before deferring recovery.
Polling stop/disarm and orphan client close run on the transport loop. Webhook
serving retains its original gateway queue.


The transport loop has a separate two-worker executor for DNS/TLS pool work.
Inbox I/O uses its own single daemon worker, so slow disk commits cannot occupy
network construction/resolution workers or hold process exit open. Acceptance
awaits the FULL commit: an abandoned pending write has never been acknowledged.
It survives individual request shutdown/reinitialize cycles because PTB's
polling events and locks bind to that loop. Clients and transports still close
on drain, failed initialization, and disconnect. This is process-lifetime
infrastructure, not one new thread per reconnect.

Runtime status preparation uses a separate single-worker executor and coalesces
pending fields by platform. The synchronous status API remains available for CLI
callers. Profile control and shared-listener publication await preparation
cooperatively before returning. Gateway producers never wait on the status RLock or logging handler
RLocks: console writes queue to a writer thread, and the file queue handler skips
its unnecessary producer lock. Profile file-router locks remain on the logging
listener, away from transport and gateway dispatch.

An out-of-loop diagnostic probe checks every 250 ms. A delay over one second
emits a WARNING containing the monitored loop thread's Python stack, at most
once per 30 seconds. It does not exit the process or change existing liveness
recovery thresholds. `gateway.loop_watchdog: false` disables both diagnostics
and the existing lifetime guards.

Pool reset detaches and closes a failed pool before building another. Failure
diagnostics run in the I/O executor because redaction takes the vault registry
lock also used by agent tools. Read/write
transport failures also discard pools, without retrying messages whose acceptance
is ambiguous. Final close attempts every pool even when another close fails, and
closed transports refuse new fallback pools. Concurrent failures identify the
pool they failed on so they cannot discard a newer replacement.

## Limits and follow-up

Threads still share the Python GIL and the host scheduler. This change protects
network progress against gateway-thread lock waits and blocking agent dispatch;
it does not guarantee latency on a host that cannot schedule Python threads.
A follow-up process split should put Bot API clients, polling, and outbound
requests in a small transport process, with bounded update and command queues,
explicit cancellation and acknowledgements, profile-scoped credential delivery,
and token-lock ownership. The parent should retain authorization, session
routing, and agent execution. Test crash/restart and duplicate update admission
before replacing the thread boundary.

## Review and rollout

The saved native sample proves a synchronous RLock wait but lacks Python frames;
it cannot attribute the October incident to a particular lock. Regression tests
hold the runtime-status and console-handler locks from other threads, exercise
real localhost PTB polling/sends, and inspect actual TCP connections after
repeated pool resets and failures. Tests use fake tokens and never contact real
Telegram chats.

After review and merge to fork `prod`, the coordinator installs the reviewed
revision through the managed workflow:

```sh
hermes update --remote fork --branch prod --sha <reviewed-merged-sha> --no-gateway-restart
hermes gateway restart
```

Do not point the
live service at the development worktree. Verify the installed/live SHA,
readiness receipt, `getUpdates progressing`, and no growth of Telegram sockets
through reconnects. Keep the previous installed generation available for
rollback. Source changes alone do not change the running gateway.
