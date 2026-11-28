"""An ACP editor survives a gateway restart: real websockets, the real GatewayClient, two owners."""
import asyncio
from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import acp
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import acp_adapter.gateway_server as server
from acp_adapter.gateway_server import GatewayACPAgent
from hermes_cli import gateway_client
from hermes_cli.gateway_client import DISCONNECTED, NOT_SENT, GatewayClient, GatewayClientError


class Owner:
    """One gateway instance: answers the canonical RPCs ACP uses and records them."""

    def __init__(self, profile_id, epoch, *, die_on_submit=None, prior=None, receipt_status=None, emit_prefix=True):
        self.profile_id, self.epoch, self.die_on_submit = profile_id, epoch, die_on_submit
        self.prior, self.receipt_status, self.emit_prefix = prior, receipt_status, emit_prefix
        self.calls, self.configs, self.inputs, self.lookups = [], [], [], []
        self.server = None
        self.lookup_started, self.release_receipt = asyncio.Event(), asyncio.Event()

    async def complete(self, ws, session_id, admission_id, seq, text, *, outcome="completed"):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
            "session_id": session_id, "type": "message.complete", "replay_epoch": self.epoch,
            "seq": seq, "admission_id": admission_id,
            "payload": {"outcome": outcome, "text": text}}}))

    async def delta(self, ws, session_id, admission_id, seq, text):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
            "session_id": session_id, "type": "message.delta", "replay_epoch": self.epoch,
            "seq": seq, "admission_id": admission_id, "payload": {"text": text}}}))

    async def refuse(self, ws, frame, reason):
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "error": {
            "code": -32603, "message": "Internal error", "data": {"reason": reason}}}))

    async def reply(self, ws, frame, result):
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))

    async def resume(self, ws, frame):
        session_id = frame["params"]["session_id"]
        if self.receipt_status == "expired_session" and session_id == "s":
            await self.refuse(ws, frame, "not_found")
            return
        snapshot = {"session_id": session_id, "messages": [], "pending": [], "prompts": [],
                    "replay_epoch": self.epoch, "last_sequence": 0}
        if self.receipt_status not in {"missed_prefix", "queued_then_started"}:
            await self.reply(ws, frame, snapshot)
            return
        original_admission = f"{self.prior.epoch}-{self.prior.inputs[0][1]}"
        status = "started" if self.receipt_status == "missed_prefix" else "queued"
        snapshot["pending"] = [{"status": status, "admission_id": original_admission}]
        await self.reply(ws, frame, snapshot)
        if status == "queued":
            # The authority subscribes before this claim, so the replacement sees its first chunk.
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                "session_id": session_id, "type": "session.info", "replay_epoch": self.epoch,
                "seq": 1, "admission_id": original_admission, "payload": {"pending": [
                    {"status": "started", "admission_id": original_admission}]}}}))
            await self.delta(ws, session_id, original_admission, 2, "ORI")
            await self.delta(ws, session_id, original_admission, 3, "GINAL")
        else:
            await self.delta(ws, session_id, original_admission, 1, "GINAL")

    async def submit(self, ws, frame):
        params = frame["params"]
        session_id, input_id = params["session_id"], params["input_id"]
        self.inputs.append((session_id, input_id))
        if self.die_on_submit == "before_ack":
            await ws.close()  # the owner accepted the input and lost the ack
            return
        admission_id = f"{self.epoch}-{input_id}"
        await self.reply(ws, frame, {"admission_id": admission_id})
        if self.die_on_submit == "after_ack":
            if self.emit_prefix:
                await self.delta(ws, session_id, admission_id, 1, "ORI")
            await ws.close()
            return
        await self.complete(ws, session_id, admission_id, 5, "INDEPENDENT")

    async def receipt(self, ws, frame):
        params = frame["params"]
        self.lookups.append(params)
        if self.receipt_status in {"cancelled_lookup", "receipt_timeout"}:
            self.lookup_started.set()
            await self.release_receipt.wait()
        if self.receipt_status == "receipt_disconnect":
            await ws.close()
            return
        if self.receipt_status == "missing_admission":
            await self.refuse(ws, frame, "not_found")
            return
        original_session, original_input = self.prior.inputs[0]
        original_admission = f"{self.prior.epoch}-{original_input}"
        assert params["session_id"] == original_session
        assert params["include_result"] is True
        if self.prior.die_on_submit == "before_ack":
            assert params["input_id"] == original_input and "admission_id" not in params
        else:
            assert params["admission_id"] == original_admission and "input_id" not in params
        started = self.receipt_status in {
            "started", "started_then_disconnect", "started_then_steered", "missed_prefix", "queued_then_started",
        }
        result = {
            "admission_id": original_admission,
            "status": "started" if started else "terminal",
            "outcome": "completed" if self.receipt_status.startswith("completed") else self.receipt_status,
        }
        if self.receipt_status in {"completed", "completed_with_usage"}:
            result["result"] = {"final_response": "ORIGINAL"}
            result["usage"] = {"total_tokens": 11} if self.receipt_status == "completed_with_usage" else {}
        await self.reply(ws, frame, result)
        if self.receipt_status == "started_then_disconnect":
            await ws.close()
            return
        if self.receipt_status == "started":
            await self.complete(ws, original_session, "newer-admission", 1, "NEWER", outcome="interrupted")
        if self.receipt_status == "started_then_steered":
            await self.complete(ws, original_session, original_admission, 2, "", outcome="steered")
            return
        if started:
            sequence = 4 if self.receipt_status == "queued_then_started" else 2
            await self.complete(ws, original_session, original_admission, sequence, "ORIGINAL")

    async def handler(self, ws):
        handlers = {"session.resume": self.resume, "prompt.submit": self.submit, "prompt.receipt": self.receipt}
        async for raw in ws:
            frame = json.loads(raw)
            method = frame["method"]
            self.calls.append(method)
            if method in handlers:
                await handlers[method](ws, frame)
            else:
                if method == "config.set":
                    self.configs.append(frame["params"])
                result = {"profile_id": self.profile_id} if method == "runtime.describe" else {}
                await self.reply(ws, frame, result)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("die", ["before_ack", "after_ack"])
@pytest.mark.parametrize(("receipt_status", "stop_reason"), [
    ("completed", "end_turn"),
    ("completed_with_usage", "end_turn"),
    ("interrupted", "cancelled"),
    ("steered", "end_turn"),
    ("started_then_steered", "end_turn"),
    ("failed", "admitted_turn_failed"),
    ("rejected", "admitted_turn_failed"),
    ("started", "end_turn"),
    ("missed_prefix", "end_turn"),
    ("queued_then_started", "end_turn"),
    ("missing_admission", None),
    ("expired_session", None),
    ("receipt_disconnect", None),
    ("cancelled_lookup", None),
    ("receipt_timeout", None),
    ("started_then_disconnect", None),
    ("completed_without_result", None),
])
async def test_reconcile_original_turn_without_resending(monkeypatch, die, receipt_status, stop_reason):
    if receipt_status == "receipt_timeout":
        monkeypatch.setattr(gateway_client, "DEFAULT_RPC_TIMEOUT", 2.0)
    agent = GatewayACPAgent()
    agent._conn = AsyncMock()
    home = str(agent._home)
    first = Owner(home, "first", die_on_submit=die, emit_prefix=receipt_status not in {
        "missed_prefix", "queued_then_started", "steered", "started_then_steered",
    })
    second = Owner(home, "second", prior=first, receipt_status=receipt_status)
    third = Owner(home, "third")
    owners = [first, second, third]
    for owner in owners:
        owner.server = await serve(owner.handler, "127.0.0.1", 0)
    order = iter(owners)

    @asynccontextmanager
    async def connect_gateway():
        async with connect(next(order).url) as ws:
            async with GatewayClient(ws) as client:
                yield client

    monkeypatch.setattr(server, "connect_gateway", connect_gateway)
    try:
        await agent.load_session(cwd="/", session_id="s")
        agent._usage["s"] = {"total_tokens": 7}
        agent._conn.session_update.reset_mock()
        prompt = [acp.text_block("synthetic prompt")]
        if stop_reason is None:
            with pytest.raises(GatewayClientError, match=r"unknown_execution: .* outcome is unknown.* Session s,") as unknown:
                if receipt_status in {"cancelled_lookup", "receipt_timeout"}:
                    original = asyncio.create_task(agent.prompt(prompt, "s"))
                    await asyncio.wait_for(second.lookup_started.wait(), 5)
                    if receipt_status == "cancelled_lookup":
                        original.cancel()
                    await asyncio.wait_for(original, 5)
                else:
                    await asyncio.wait_for(agent.prompt(prompt, "s"), 5)
            assert first.inputs[0][1] in str(unknown.value)
        elif stop_reason == "admitted_turn_failed":
            with pytest.raises(GatewayClientError, match="^admitted_turn_failed$"):
                await asyncio.wait_for(agent.prompt(prompt, "s"), 5)
        else:
            response = await asyncio.wait_for(agent.prompt(prompt, "s"), 5)
            assert response.stop_reason == stop_reason
            expected_usage = {"completed": 7, "completed_with_usage": 11}.get(receipt_status)
            if expected_usage is not None:
                assert response.usage.total_tokens == expected_usage
        assert first.calls.count("prompt.submit") == 1
        assert second.calls.count("prompt.submit") == third.calls.count("prompt.submit") == 0
        assert second.calls.count("prompt.receipt") == (0 if receipt_status == "expired_session" else 1)
        assert not agent._unacked
        second.release_receipt.set()

        # A newly requested prompt with identical content gets a fresh identity and runs once.
        if receipt_status == "expired_session":
            await agent.load_session(cwd="/", session_id="other-session")
            session_id = "other-session"
        else:
            session_id = "s"
        response = await asyncio.wait_for(agent.prompt(prompt, session_id), 5)
        assert response.stop_reason == "end_turn"
        [independent] = second.inputs + third.inputs
        assert independent[1] != first.inputs[0][1]
        texts = [call.kwargs["update"].content.text for call in agent._conn.session_update.await_args_list
                 if call.kwargs["update"].session_update == "agent_message_chunk"]
        assert texts.count("INDEPENDENT") == 1
        if receipt_status in {"completed", "completed_with_usage", "started", "missed_prefix", "queued_then_started"}:
            assert "".join(text for text in texts if text not in {"NEWER", "INDEPENDENT"}) == "ORIGINAL"
    finally:
        second.release_receipt.set()
        await agent.aclose()
        for owner in owners:
            owner.server.close()
            await owner.server.wait_closed()


@pytest.mark.asyncio
async def test_stop_during_ack_is_delivered_to_the_original_admission_after_disconnect(monkeypatch):
    agent = GatewayACPAgent()
    agent._pending_cancels.add("s")
    agent._admissions["s"] = "original"
    first, replacement = AsyncMock(), AsyncMock()
    first.rpc.side_effect = GatewayClientError(DISCONNECTED)
    receipt = {"admission_id": "original", "status": "started"}
    monkeypatch.setattr(agent, "_reconcile_disconnect", AsyncMock(return_value=(replacement, 1, receipt)))

    async def terminal(*_):
        return {"outcome": "cancelled" if replacement.rpc.await_count else "completed"}

    monkeypatch.setattr(agent, "_wait_for_terminal", terminal)

    result, recovered = await agent._observe_admitted_prompt(
        first, "s", "s", "input", "original", 0, 0, receipt, recovered=False)

    for client in (first, replacement):
        client.rpc.assert_awaited_once_with("prompt.cancel", session_id="s", admission_id="original")
    assert recovered and result["outcome"] == "cancelled"
    assert not agent._pending_cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["attach", "submit"])
async def test_disconnect_before_socket_write_does_not_reconcile_or_retry(monkeypatch, stage):
    agent = GatewayACPAgent()
    agent._snapshots["s"] = {"pending": []}
    agent._gateway = AsyncMock()
    agent._gateway.rpc.side_effect = GatewayClientError(NOT_SENT)
    if stage == "attach":
        monkeypatch.setattr(agent, "_client", AsyncMock(side_effect=GatewayClientError(DISCONNECTED)))

    with pytest.raises(GatewayClientError, match="nothing was sent"):
        await agent.prompt([acp.text_block("synthetic prompt")], "s")

    assert [call.args[0] for call in agent._gateway.rpc.await_args_list] == (
        ["prompt.submit"] if stage == "submit" else [])
    assert not agent._unacked


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["reconnect", "gap"])
async def test_a_recovered_snapshot_repaints_the_sessions_edit_mode(path):
    """ACP-7: a reconnect or replay gap adopts a fresh snapshot and skips the events before it, so the
    snapshot's own edit policy is what the editor is shown (owner Don't Ask is never left as Default)."""
    agent = GatewayACPAgent()
    agent._conn = AsyncMock()
    agent._snapshots["s"] = {"pending": [], "replay_epoch": "old", "last_sequence": 0}
    agent._modes["s"] = "default"
    snapshot = {"session_id": "s", "pending": [], "prompts": [], "replay_epoch": "new", "last_sequence": 99,
                "info": {"edit_approval_policy": "session"}}

    async def rpc(method, **kwargs):
        assert method in {"session.resume", "config.set"}, method
        return snapshot if method == "session.resume" else {}

    agent._gateway = type("Gateway", (), {"rpc": staticmethod(rpc)})()
    try:
        if path == "reconnect":
            await agent._reattach(agent._gateway)
        else:
            await agent._gap_guard("session.replay_gap", "s", None)
        modes = [call.kwargs["update"].current_mode_id for call in agent._conn.session_update.await_args_list
                 if call.kwargs["update"].session_update == "current_mode_update"]
        assert agent._modes["s"] == "dont_ask" and modes == ["dont_ask"]
    finally:
        for task in agent._background:
            task.cancel()
