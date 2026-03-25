"""SDK for pwncollege dojo"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _extract_csrf_nonce(html: str) -> str | None:
    match = re.search(r"'csrfNonce': \"([^\"]+)\"", html)
    return match.group(1) if match else None


@dataclass
class RLInstance:
    slot: int
    ssh_user: str
    challenge_id: str
    module_id: str
    dojo_id: str
    flag: str | None = None
    created_at: float | None = None
    status: str | None = None

    @property
    def challenge_key(self) -> str:
        return f"{self.module_id}/{self.challenge_id}"


@dataclass
class RLResource:
    type: str
    name: str
    content: str | None = None
    video: str | None = None
    slides: str | None = None


@dataclass
class RLChallenge:
    id: str
    name: str
    description: str
    module_id: str | None = None
    module_name: str | None = None
    module_description: str | None = None
    dojo_id: str | None = None
    dojo_name: str | None = None
    dojo_description: str | None = None
    resources: list[RLResource] = field(default_factory=list)

    @property
    def challenge_key(self) -> str | None:
        if self.module_id:
            return f"{self.module_id}/{self.id}"
        return None


@dataclass
class RLStatus:
    enabled: bool
    max_instances: int
    running: int
    instances: list[RLInstance]


class DojoRLClient:
    """Client for the dojo RL API. No auth required."""

    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self.client.aclose()

    def _rl_url(self, path: str) -> str:
        return f"/pwncollege_api/v1/rl{path}"

    async def _get(self, path: str) -> dict[str, Any]:
        resp = await self.client.get(self._rl_url(path))
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> dict[str, Any]:
        resp = await self.client.post(self._rl_url(path), json=json or {})
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict[str, Any]:
        resp = await self.client.delete(self._rl_url(path))
        resp.raise_for_status()
        return resp.json()

    # ── Response Parsing ──────────────────────────────────────────────────────
    # The API uses different field names in create/reset vs get/list responses.
    # These parsers normalize everything into RLInstance.

    @staticmethod
    def _parse_create_response(data: dict[str, Any]) -> RLInstance:
        return RLInstance(
            slot=data["slot"],
            ssh_user=data["ssh_user"],
            challenge_id=data["challenge"],
            module_id=data["module"],
            dojo_id=data["dojo"],
        )

    @staticmethod
    def _parse_instance_detail(data: dict[str, Any]) -> RLInstance:
        created_at = data.get("created_at")
        return RLInstance(
            slot=data["slot"],
            ssh_user=data.get("ssh_user", f"rl_{data['slot']}"),
            challenge_id=data["challenge_id"],
            module_id=data["module_id"],
            dojo_id=data["dojo_id"],
            flag=data.get("flag"),
            created_at=float(created_at) if created_at else None,
        )

    @staticmethod
    def _parse_instance_listing(data: dict[str, Any]) -> RLInstance:
        created_at = data.get("created_at")
        return RLInstance(
            slot=data["slot"],
            ssh_user=f"rl_{data['slot']}",
            challenge_id=data["challenge_id"],
            module_id=data["module_id"],
            dojo_id=data["dojo_id"],
            created_at=float(created_at) if created_at else None,
            status=data.get("status"),
        )

    @staticmethod
    def _parse_challenge(data: dict[str, Any]) -> RLChallenge:
        resources = [
            RLResource(
                type=r["type"],
                name=r["name"],
                content=r.get("content"),
                video=r.get("video"),
                slides=r.get("slides"),
            )
            for r in data.get("resources", [])
        ]
        return RLChallenge(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            module_id=data.get("module_id"),
            module_name=data.get("module_name"),
            module_description=data.get("module_description"),
            dojo_id=data.get("dojo_id"),
            dojo_name=data.get("dojo_name"),
            dojo_description=data.get("dojo_description"),
            resources=resources,
        )

    # ── RL Instance Lifecycle ─────────────────────────────────────────────────

    async def status(self) -> RLStatus:
        result = await self._get("/status")
        instances = [
            self._parse_instance_listing(inst) for inst in result.get("instances", [])
        ]
        return RLStatus(
            enabled=result["enabled"],
            max_instances=result["max_instances"],
            running=result["running"],
            instances=instances,
        )

    async def create_instance(
        self, challenge: str, *, variant: int | None = None
    ) -> RLInstance:
        data: dict[str, Any] = {"challenge": challenge}
        if variant is not None:
            data["variant"] = variant
        result = await self._post("/instances", json=data)
        if not result.get("success"):
            raise RuntimeError(f"Failed to create instance: {result.get('error')}")
        return self._parse_create_response(result)

    async def get_instance(self, slot: int) -> RLInstance:
        result = await self._get(f"/instances/{slot}")
        if not result.get("success"):
            raise KeyError(f"No instance at slot {slot}")
        return self._parse_instance_detail(result)

    async def list_instances(self) -> list[RLInstance]:
        result = await self._get("/instances")
        return [
            self._parse_instance_listing(inst) for inst in result.get("instances", [])
        ]

    async def destroy_instance(self, slot: int) -> None:
        result = await self._delete(f"/instances/{slot}")
        if not result.get("success"):
            raise RuntimeError(f"Failed to destroy instance: {result.get('error')}")

    async def reset_instance(
        self, slot: int, *, challenge: str | None = None
    ) -> RLInstance:
        data: dict[str, Any] = {}
        if challenge is not None:
            data["challenge"] = challenge
        result = await self._post(f"/instances/{slot}/reset", json=data)
        if not result.get("success"):
            raise RuntimeError(f"Failed to reset instance: {result.get('error')}")
        return self._parse_create_response(result)

    async def check_flag(self, slot: int, flag: str) -> bool:
        result = await self._post(f"/instances/{slot}/check", json={"flag": flag})
        return result.get("correct", False)

    async def get_flag(self, slot: int) -> str:
        instance = await self.get_instance(slot)
        if instance.flag is None:
            raise RuntimeError(f"No flag available for slot {slot}")
        return instance.flag

    # ── Challenge Discovery ───────────────────────────────────────────────────

    async def list_challenges(self) -> list[RLChallenge]:
        result = await self._get("/challenges")
        return [self._parse_challenge(ch) for ch in result.get("challenges", [])]

    # ── Admin (requires auth) ─────────────────────────────────────────────────

    async def admin_login(
        self, username: str = "admin", password: str = "admin"
    ) -> None:
        resp = await self.client.get("/login")
        nonce = _extract_csrf_nonce(resp.text)
        if not nonce:
            raise RuntimeError("Could not extract CSRF nonce")
        self._admin_csrf = nonce
        resp = await self.client.post(
            "/login",
            data={"name": username, "password": password, "nonce": nonce},
        )
        if resp.status_code not in (200, 302):
            raise RuntimeError(f"Login failed: {resp.status_code}")
        resp = await self.client.get("/")
        self._admin_csrf = _extract_csrf_nonce(resp.text) or self._admin_csrf

    async def load_dojo(self, repository: str) -> str:
        if not hasattr(self, "_admin_csrf"):
            raise RuntimeError("Must call admin_login() first")
        resp = await self.client.post(
            "/pwncollege_api/v1/dojos/create",
            json={
                "repository": repository,
                "public_key": f"public/{repository}",
                "private_key": f"private/{repository}",
            },
            headers={"CSRF-Token": self._admin_csrf},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"Failed to load dojo: {data.get('error', data)}")
        return data.get("dojo", repository)

    async def promote_dojo(self, dojo_id: str) -> None:
        if not hasattr(self, "_admin_csrf"):
            raise RuntimeError("Must call admin_login() first")
        resp = await self.client.post(
            f"/pwncollege_api/v1/dojos/{dojo_id}/promote",
            json={},
            headers={"CSRF-Token": self._admin_csrf},
        )
        resp.raise_for_status()

    # ── Bulk Operations ───────────────────────────────────────────────────────

    async def create_batch(self, challenge: str, count: int) -> list[RLInstance]:
        tasks = [self.create_instance(challenge) for _ in range(count)]
        return await asyncio.gather(*tasks)

    async def destroy_all(self) -> int:
        instances = await self.list_instances()
        for inst in instances:
            await self.destroy_instance(inst.slot)
        return len(instances)


class DojoRLSyncClient:
    """Sync wrapper for DojoRLClient.

    Runs all async operations on a dedicated background thread with its own
    event loop, so it's safe to call from any context — including from inside
    another running event loop (e.g., Atropos's loop or tool dispatch threads).
    """

    def __init__(self, base_url: str, timeout: float = 120.0):
        import threading

        self._async = DojoRLClient(base_url, timeout)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
        )
        self._thread.start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self._loop.is_running():
            return
        try:
            self._run(self._async.close())
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def status(self) -> RLStatus:
        return self._run(self._async.status())

    def create_instance(
        self, challenge: str, *, variant: int | None = None
    ) -> RLInstance:
        return self._run(self._async.create_instance(challenge, variant=variant))

    def get_instance(self, slot: int) -> RLInstance:
        return self._run(self._async.get_instance(slot))

    def list_instances(self) -> list[RLInstance]:
        return self._run(self._async.list_instances())

    def destroy_instance(self, slot: int) -> None:
        return self._run(self._async.destroy_instance(slot))

    def reset_instance(self, slot: int, *, challenge: str | None = None) -> RLInstance:
        return self._run(self._async.reset_instance(slot, challenge=challenge))

    def check_flag(self, slot: int, flag: str) -> bool:
        return self._run(self._async.check_flag(slot, flag))

    def get_flag(self, slot: int) -> str:
        return self._run(self._async.get_flag(slot))

    def list_challenges(self) -> list[RLChallenge]:
        return self._run(self._async.list_challenges())

    def admin_login(self, username: str = "admin", password: str = "admin") -> None:
        return self._run(self._async.admin_login(username, password))

    def load_dojo(self, repository: str) -> str:
        return self._run(self._async.load_dojo(repository))

    def promote_dojo(self, dojo_id: str) -> None:
        return self._run(self._async.promote_dojo(dojo_id))

    def destroy_all(self) -> int:
        return self._run(self._async.destroy_all())


@dataclass
class EpisodePool:
    """Manages a pool of RL instances for parallel episode collection."""

    client: DojoRLClient
    challenge: str
    pool_size: int = 32
    acquisition_timeout: float = 300.0

    _available: asyncio.Queue[RLInstance] = field(
        default_factory=asyncio.Queue, init=False
    )
    _all_instances: dict[int, RLInstance] = field(default_factory=dict, init=False)
    _initialized: bool = field(default=False, init=False)

    async def initialize(self) -> None:
        if self._initialized:
            return
        for _ in range(self.pool_size):
            instance = await self.client.create_instance(self.challenge)
            full = await self.client.get_instance(instance.slot)
            self._all_instances[instance.slot] = full
            await self._available.put(full)
        self._initialized = True

    @asynccontextmanager
    async def acquire(self):
        if not self._initialized:
            raise RuntimeError("EpisodePool not initialized")
        try:
            instance = await asyncio.wait_for(
                self._available.get(), timeout=self.acquisition_timeout
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"No instance available within {self.acquisition_timeout}s"
            )
        try:
            yield instance
        finally:
            try:
                reset = await self.client.reset_instance(
                    instance.slot, challenge=self.challenge
                )
                full = await self.client.get_instance(reset.slot)
                self._all_instances[reset.slot] = full
                await self._available.put(full)
            except Exception as e:
                logger.error(
                    "Failed to reset instance slot %d, returning stale instance: %s",
                    instance.slot,
                    e,
                )
                await self._available.put(instance)

    async def shutdown(self) -> None:
        errors = []
        for slot in list(self._all_instances.keys()):
            try:
                await self.client.destroy_instance(slot)
            except Exception as e:
                errors.append((slot, e))
                logger.warning("Failed to destroy instance slot %d: %s", slot, e)
        self._all_instances.clear()
        self._initialized = False
        if errors:
            logger.error(
                "EpisodePool shutdown: %d instance(s) failed to destroy", len(errors)
            )
