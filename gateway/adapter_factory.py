"""Off-loop preparation for gateway adapter lifecycle paths."""
import asyncio

from gateway.config import Platform


async def create_adapter_async(runner, platform, config):
    """Keep disk/dependency work out of reconnect and profile-reload tasks.

    Telegram's synchronous constructor has no loop affinity. Other plugin factories can
    create loop-owned resources, so only their load/probe/validation phase runs in a worker.
    """
    if platform is Platform.TELEGRAM:
        return await asyncio.to_thread(runner._create_adapter, platform, config)
    from gateway.platform_registry import platform_registry
    if not platform_registry.is_registered(platform.value):
        return await asyncio.to_thread(runner._create_adapter, platform, config)
    runner._seed_adapter_config(config)
    entry = await asyncio.to_thread(platform_registry.prepare_adapter, platform.value, config)
    if entry is None:
        return None
    adapter = platform_registry.build_adapter(entry, config)
    if adapter is not None:
        adapter.gateway_runner = runner
    return adapter


async def run_in_adapter_profile(home, fn, *args):
    """Prepare profile secrets/terminal policy off-loop; connect on the gateway loop in that context."""
    from contextvars import copy_context
    from gateway.run import _profile_runtime_scope

    def prepare():
        with _profile_runtime_scope(home, hydrate_secrets=False):
            return copy_context()

    context = await asyncio.to_thread(prepare)
    return await asyncio.create_task(fn(*args), context=context)


def flag_secondary_reconnect_attention(runner, profile_name, platform, info):
    """Live config/secret reads for escalation run in the reconnect worker too."""
    import functools
    import time
    from gateway.run import _profile_runtime_scope

    home = runner._routed_profile_home(profile_name)
    with runner._scope_or_null(functools.partial(_profile_runtime_scope, hydrate_secrets=False), home):
        runner._flag_reconnect_needs_attention(
            platform, info, time.monotonic(), status_key=f"{profile_name}:{platform.value}")


async def acquire_adapter_lock(adapter, *args):
    """Acquire off-loop; a canceled connect must release a lock that the worker acquires late."""
    task = asyncio.create_task(asyncio.to_thread(adapter._acquire_platform_lock, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        async def release_late():
            if await task:
                await asyncio.to_thread(adapter._release_platform_lock)

        cleanup = asyncio.create_task(release_late())
        adapter._detached_fatal_tasks.add(cleanup)
        cleanup.add_done_callback(adapter._detached_fatal_tasks.discard)
        from agent.async_utils import consume_detached_task_result
        cleanup.add_done_callback(consume_detached_task_result)
        raise
