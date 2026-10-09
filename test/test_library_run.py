"""Vendored aiopulse2: the websocket runner, heartbeat and shutdown, with a fake socket."""

import asyncio
import contextlib
import json
from unittest.mock import patch

import pytest
from websockets.protocol import State

from custom_components.automate.vendor.aiopulse2 import devices, errors
from custom_components.automate.vendor.aiopulse2.devices import Hub

from .conftest import HUB_HOST, HUB_ID, shadow_message


class FakeWebSocket:
    """What `async with websockets.connect(...)` yields, scripted per connection."""

    def __init__(self, messages=(), fail_connect=None):
        self.messages = list(messages)
        self.fail_connect = fail_connect
        self.sent = []
        self.state = State.OPEN
        self.closed = False
        self.release = asyncio.Event()

    async def __aenter__(self):
        if self.fail_connect:
            raise self.fail_connect
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        await self.release.wait()  # hold the connection open until released
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.state = State.CLOSED
        self.release.set()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(devices, "RECONNECT_MIN", 0.02)
    monkeypatch.setattr(devices, "RECONNECT_MAX", 0.08)


@contextlib.asynccontextmanager
async def running(hub, sockets):
    """Run the hub against scripted sockets, no real network, always stopped."""
    connect = patch.object(devices.websockets, "connect", side_effect=list(sockets))
    with connect, patch.object(devices.Hub, "runserial"):
        task = asyncio.create_task(hub.run())
        try:
            yield task
        finally:
            if hub.running:
                await hub.stop()
            for sock in sockets:
                sock.release.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task, 2)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never met"
        await asyncio.sleep(0.01)


async def test_reconnect_backs_off_then_connects(fast):
    sockets = [
        FakeWebSocket(fail_connect=OSError("refused")),
        FakeWebSocket(fail_connect=OSError("refused")),
        FakeWebSocket(fail_connect=OSError("refused")),
        FakeWebSocket([shadow_message({"ABC": {"rs": -82, "ol": True}})]),
    ]
    hub = Hub(HUB_HOST, signal_poll_interval=0, poll_interval=1000)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with running(hub, sockets) as task:
        await _wait_for(lambda: hub.connected)
        elapsed = loop.time() - started
        # three failures: 0.02 + 0.04 + 0.08 = 0.14 s of back-off before success
        assert 0.14 <= elapsed < 1.0
        assert hub.rollers["ABC"].signal == -82
        assert hub.lasterrorlog is None, "the 'Connected to' line was logged and cleared"
        await hub.stop()
        await asyncio.wait_for(task, 2)
    assert hub.heartbeat_task is None
    assert hub.running is False


async def test_heartbeat_sends_shadow_poll_at_the_configured_interval():
    sock = FakeWebSocket([shadow_message({"ABC": {"rs": -82, "ol": True}})])
    hub = Hub(HUB_HOST, signal_poll_interval=0, poll_interval=2)
    hub.heartbeatinterval = 0.05  # tests only; the floor applies to real callers
    async with running(hub, [sock]):
        await asyncio.sleep(0.3)
        polls = [m for m in sock.sent if m.get("src") == "app" and "args" not in m]
        assert 3 <= len(polls) <= 8
        assert all(m["method"] == "shadow" for m in polls)
        # The queued details query went out ahead of a poll, exactly once
        queries = [m for m in sock.sent if "args" in m]
        assert len(queries) == 1
        assert queries[0]["args"]["desired"] == {"shades": {"ABC": {"query": True}}}
        assert "movePercent" not in json.dumps(sock.sent)
        assert "stopShade" not in json.dumps(sock.sent)
    assert sock.closed


async def test_clean_close_by_hub_reconnects_and_marks_disconnected(fast):
    first = FakeWebSocket([shadow_message({"ABC": {"rs": -82, "ol": True}})])
    second = FakeWebSocket([shadow_message({"ABC": {"rs": -70, "ol": True}})])
    # propagate_callbacks=True is how the integration constructs it: a hub-level
    # change (here: the disconnect) is pushed to every roller's subscribers.
    hub = Hub(HUB_HOST, signal_poll_interval=0, poll_interval=1000, propagate_callbacks=True)
    seen = []
    async with running(hub, [first, second]):
        await _wait_for(lambda: "ABC" in hub.rollers)
        hub.rollers["ABC"].name = "Kitchen"  # release the name gate
        hub.rollers["ABC"].callback_subscribe(lambda r: seen.append(hub.connected))
        first.release.set()  # the hub closes the socket cleanly
        await _wait_for(lambda: hub.rollers["ABC"].signal == -70)
        await asyncio.sleep(0)
        assert False in seen, "entities were told the hub was disconnected"
        assert hub.connected is True


async def test_send_payload_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(devices, "SEND_HANDSHAKE_TIMEOUT", 0.05)
    hub = Hub(HUB_HOST, signal_poll_interval=0)
    hub.running = True
    with pytest.raises(errors.NotConnectedException):
        await hub.send_payload({"method": "shadow"})
    hub.running = False
    with pytest.raises(errors.NotRunningException):
        await hub.send_payload({"method": "shadow"})


async def test_test_connection_cleans_up_background_tasks():
    sock = FakeWebSocket([shadow_message({"ABC": {"rs": -82, "ol": True}})])
    hub = Hub(HUB_HOST)
    with (
        patch.object(devices.websockets, "connect", return_value=sock),
        patch.object(devices.Hub, "runserial"),
    ):
        assert await hub.test() is True
    assert hub.id == HUB_ID
    assert hub.name == "Living room hub"
    assert hub.running is False
    assert hub.heartbeat_task is None and hub.signal_task is None
    assert hub.connected is False
    assert sock.closed


async def test_test_connection_rejects_a_hub_that_never_sends_a_shadow():
    sock = FakeWebSocket([json.dumps({"error": {"code": -1}})])
    hub = Hub(HUB_HOST)
    with patch.object(devices.websockets, "connect", return_value=sock):
        with pytest.raises(errors.InvalidResponseException):
            await hub.test()
    assert hub.heartbeat_task is None
    assert hub.running is False
