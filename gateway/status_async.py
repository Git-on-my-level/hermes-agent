"""Keep status preparation (locks, identity probes) off asyncio.

Status is a snapshot, so a blocked preparer retains only the latest fields per
platform instead of growing an executor queue on every retry/heartbeat.
"""
import asyncio
from tools.daemon_pool import DaemonThreadPoolExecutor
from contextvars import copy_context
import logging

logger = logging.getLogger(__name__)
_executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix='gateway-status-prepare')
_batches = {}


def publish_runtime_status(**fields):
    from gateway.status import publish_runtime_status as publish
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return publish(**fields)
    batch = _batches.get(loop)
    if batch is None:
        batch = {'pending': {}, 'task': None}
        _batches[loop] = batch
    key = (fields.get('platform'), fields.get('drop_profile_platforms'), fields.get('clear_profile_platforms'))
    # A later update must stay after structural drops/clears queued between
    # two snapshots of the same platform. Preserve last-submission order.
    previous = batch['pending'].pop(key, None)
    merged = {**(previous[1] if previous else {}), **fields}
    batch['pending'][key] = (copy_context(), merged)
    if batch['task'] is None:
        async def drain():
            try:
                while batch['pending']:
                    key = next(iter(batch['pending']))
                    context, snapshot = batch['pending'].pop(key)
                    try:
                        await loop.run_in_executor(_executor, context.run, lambda: publish(**snapshot))
                    except Exception as exc:
                        logger.warning('Runtime status preparation failed: %s', exc)
            finally:
                _batches.pop(loop, None)
        batch['task'] = loop.create_task(drain())
    return batch['task']


async def flush_pending():
    batch = _batches.get(asyncio.get_running_loop())
    if batch is not None:
        await asyncio.shield(batch['task'])
