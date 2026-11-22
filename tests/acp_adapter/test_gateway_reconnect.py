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
from hermes_cli.gateway_client import DISCONNECTED, NOT_SENT, GatewayClient, GatewayClientError


class Owner:
    """One gateway instance: answers the canonical RPCs ACP uses and records them."""

    def __init__(self, profile_id, epoch, *, die_on_submit=None, prior=None, receipt_status=None):
        self.profile_id, self.epoch, self.die_on_submit = profile_id, epoch, die_on_submit
        self.prior, self.receipt_status = prior, receipt_status
        self.calls, self.configs, self.inputs, self.lookups = [], [], [], []
        self.server = None

    async def complete(self, ws, session_id, admission_id, seq, text, *, outcome="completed"):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
            "session_id": session_id, "type": "message.complete", "replay_epoch": self.epoch,
            "seq": seq, "admission_id": admission_id,
            "payload": {"outcome": outcome, "text": text}}}))

    async def refuse(self, ws, frame, reason):
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "error": {
            "code": -32603, "message": "Internal error", "data": {"reason": reason}}}))

    async def handler(self, ws):
        async for raw in ws:
            frame = json.loads(raw)
            method, params = frame["method"], frame["params"]
            self.calls.append(method)
            if method == "config.set":
                self.configs.append(params)
            result = {"profile_id": self.profile_id} if method == "runtime.describe" else {}
            if method == "session.resume":
                if self.receipt_status == "expired_session" and params["session_id"] == "s":
                    await self.refuse(ws, frame, "not_found")
                    continue
                result = {"session_id": params["session_id"], "messages": [], "pending": [], "prompts": [],
                          "replay_epoch": self.epoch, "last_sequence": 0}
            if method == "prompt.submit":
                input_id = params["input_id"]
                self.inputs.append((params["session_id"], input_id))
                if self.die_on_submit == "before_ack":
                    await ws.close()  # the owner accepted the input and lost the ack
                    return
                result = {"admission_id": f"{self.epoch}-{input_id}"}
            if method == "prompt.receipt":
                self.lookups.append(params)
                if self.receipt_status == "receipt_disconnect":
                    await ws.close()
                    return
                if self.receipt_status == "missing_admission":
                    await self.refuse(ws, frame, "not_found")
                    continue
                original_session, original_input = self.prior.inputs[0]
                original_admission = f"{self.prior.epoch}-{original_input}"
                assert params["session_id"] == original_session
                assert params["include_result"] is True
                if self.prior.die_on_submit == "before_ack":
                    assert params["input_id"] == original_input and "admission_id" not in params
                else:
                    assert params["admission_id"] == original_admission and "input_id" not in params
                result = {
                    "admission_id": original_admission,
                    "status": "started" if self.receipt_status in {"started", "started_then_disconnect"} else "terminal",
                    "outcome": "completed" if self.receipt_status == "completed_without_result" else self.receipt_status,
                }
                if self.receipt_status == "completed":
                    result["result"] = {"final_response": "ORIGINAL"}
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))
            if method == "prompt.receipt" and self.receipt_status == "started_then_disconnect":
                await ws.close()
                return
            if method == "prompt.receipt" and self.receipt_status == "started":
                await self.complete(ws, params["session_id"], "newer-admission", 1, "NEWER", outcome="interrupted")
                await self.complete(ws, params["session_id"], result["admission_id"], 2, "ORIGINAL")
            if method == "prompt.submit" and self.die_on_submit == "after_ack":
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                    "session_id": params["session_id"], "type": "message.delta", "replay_epoch": self.epoch,
                    "seq": 1, "admission_id": result["admission_id"], "payload": {"text": "ORI"}}}))
                await ws.close()
                return
            if method == "prompt.submit":
                await self.complete(ws, params["session_id"], result["admission_id"], 3, "INDEPENDENT")

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("die", ["before_ack", "after_ack"])
@pytest.mark.parametrize(("receipt_status", "stop_reason"), [
    ("completed", "end_turn"),
    ("interrupted", "cancelled"),
    ("started", "end_turn"),
    ("missing_admission", None),
    ("expired_session", None),
    ("receipt_disconnect", None),
    ("started_then_disconnect", None),
    ("completed_without_result", None),
])
async def test_reconcile_original_turn_without_resending(monkeypatch, die, receipt_status, stop_reason):
    agent = GatewayACPAgent()
    agent._conn = AsyncMock()
    home = str(agent._home)
    first = Owner(home, "first", die_on_submit=die)
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
        agent._conn.session_update.reset_mock()
        prompt = [acp.text_block("synthetic prompt")]
        if stop_reason is None:
            with pytest.raises(GatewayClientError, match=r"unknown_execution: .* outcome is unknown.* Session s,"):
                await asyncio.wait_for(agent.prompt(prompt, "s"), 5)
        else:
            response = await asyncio.wait_for(agent.prompt(prompt, "s"), 5)
            assert response.stop_reason == stop_reason
        assert first.calls.count("prompt.submit") == 1
        assert second.calls.count("prompt.submit") == third.calls.count("prompt.submit") == 0
        assert second.calls.count("prompt.receipt") == (0 if receipt_status == "expired_session" else 1)
        assert not agent._unacked

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
        if receipt_status in {"completed", "started"}:
            assert "".join(text for text in texts if text not in {"NEWER", "INDEPENDENT"}) == "ORIGINAL"
    finally:
        await agent.aclose()
        for owner in owners:
            owner.server.close()
            await owner.server.wait_closed()


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
