"""Vendored aiopulse2: the serial (port 1487) side, against a fake hub on 127.0.0.1."""

import asyncio
import time

import pytest

from custom_components.automate.vendor.aiopulse2 import devices
from custom_components.automate.vendor.aiopulse2.devices import Hub

from .conftest import shadow_message

# The HA test plugin blocks sockets; these tests talk to a fake hub on 127.0.0.1.
pytestmark = pytest.mark.usefixtures("socket_enabled")


class FakeSerialHub:
    """A TCP server speaking the hub's serial protocol, just enough for the tests."""

    def __init__(self, names=None, signal="54", answer=True, silent=()):
        self.names = names or {}
        self.signal = signal
        self.answer = answer
        # Shade ids that never answer a position query (out of RF range), while
        # the port itself and the other shades work as normal.
        self.silent = set(silent)
        self.received = []
        self.connections = 0
        self.active = 0
        self.max_concurrent = 0
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.readuntil(b";"), 2)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                cmd = data.decode()
                self.received.append(cmd)
                if not self.answer:
                    continue
                rid = cmd[1:4]
                if cmd.endswith("NAME?;"):
                    if rid in self.names:
                        writer.write(f"!{rid}NAME{self.names[rid]};".encode())
                elif cmd.endswith("r?;") and rid not in self.silent:
                    writer.write(f"!{rid}r050b000,R{self.signal};".encode())
                await writer.drain()
        finally:
            self.active -= 1
            writer.close()


@pytest.fixture(autouse=True)
def fast_serial_retries(monkeypatch):
    monkeypatch.setattr(devices, "SERIAL_READ_TIMEOUT", 0.2)
    monkeypatch.setattr(devices, "SERIAL_RETRY_MIN", 0.05)
    monkeypatch.setattr(devices, "SERIAL_RETRY_MAX", 0.1)
    monkeypatch.setattr(devices, "SIGNAL_POLL_REPLY_TIMEOUT", 0.5)


async def _hub_with_fake(fake, **kwargs):
    port = await fake.start()
    hub = Hub("127.0.0.1", serial_port=port, **kwargs)
    hub.running = True
    return hub


async def test_names_resolved_and_callbacks_released():
    fake = FakeSerialHub(names={"ABC": "Kitchen", "DEF": "Office"})
    hub = await _hub_with_fake(fake)
    hub_updates = []
    hub.callback_subscribe(lambda h: hub_updates.append(1))
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}, "DEF": {"rs": -70}}))
        assert hub_updates == [], "hub callbacks are held until names are known"
        await asyncio.wait_for(hub.rollers_known.wait(), 3)
        await asyncio.sleep(0)
        assert hub.rollers["ABC"].name == "Kitchen"
        assert hub.rollers["DEF"].name == "Office"
        assert not hub.rollers["ABC"].name_is_placeholder
        assert hub.unknown_rollers == set()
        assert len(hub_updates) >= 1
        await asyncio.wait_for(hub.serial_task, 3)
        assert hub.serialrunning is False
        assert fake.connections == 1
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_silent_serial_port_retries_then_falls_back_to_placeholder_names(caplog):
    """Upstream gave up after one 3 s silence and the integration never created an entity."""
    fake = FakeSerialHub(answer=False)
    hub = await _hub_with_fake(fake)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 5)
        assert fake.connections == devices.SERIAL_FALLBACK_ATTEMPTS
        roller = hub.rollers["ABC"]
        assert roller.name == "Shade ABC"
        assert roller.name_is_placeholder is True
        assert hub.rollers_known.is_set()
        assert hub.unknown_rollers == set()
        assert "using 'Shade ABC'" in caplog.text
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_placeholder_names_are_requeued_and_replaced_when_the_port_answers():
    fake = FakeSerialHub(answer=False)
    hub = await _hub_with_fake(fake)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 5)
        assert hub.rollers["ABC"].name == "Shade ABC"
        fake.answer = True
        fake.names["ABC"] = "Kitchen"
        hub.requeue_placeholder_names()  # what the hourly heartbeat tick does
        await asyncio.wait_for(hub.serial_task, 5)
        assert hub.rollers["ABC"].name == "Kitchen"
        assert hub.rollers["ABC"].name_is_placeholder is False
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_refused_connection_backs_off_and_falls_back(caplog):
    fake = FakeSerialHub()
    port = await fake.start()
    await fake.stop()  # nothing listens on that port now
    hub = Hub("127.0.0.1", serial_port=port)
    hub.running = True
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 5)
        assert hub.rollers["ABC"].name == "Shade ABC"
        assert "unavailable" in caplog.text
    finally:
        await hub._cancel_background_tasks()


async def test_signal_sweep_queries_one_shade_at_a_time_in_dbm():
    fake = FakeSerialHub(names={"ABC": "Kitchen", "DEF": "Office"}, signal="54")
    hub = await _hub_with_fake(fake, signal_poll_interval=10)
    hub.signal_poll_interval = 0.1  # below the floor on purpose, tests only
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}, "DEF": {"rs": -70}}))
        await asyncio.wait_for(hub.rollers_known.wait(), 3)
        hub.signal_task = asyncio.create_task(hub.signal_sweep())
        for _ in range(60):
            await asyncio.sleep(0.05)
            if hub.rollers["ABC"].serial_signal_at and hub.rollers["DEF"].serial_signal_at:
                break
        assert hub.rollers["ABC"].signal == -92
        assert hub.rollers["DEF"].signal == -92
        queries = [c for c in fake.received if c.endswith("r?;")]
        assert queries[:2] == ["!ABCr?;", "!DEFr?;"], "round-robin, one shade per tick"
        # The cached websocket value must not undo the live reading
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}, "DEF": {"rs": -70}}))
        assert hub.rollers["ABC"].signal == -92
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_sweep_waits_while_names_are_still_being_looked_up():
    fake = FakeSerialHub(names={})  # never answers names
    hub = await _hub_with_fake(fake, signal_poll_interval=10)
    hub.signal_poll_interval = 0.05
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        hub.signal_task = asyncio.create_task(hub.signal_sweep())
        await asyncio.sleep(0.3)
        assert not [c for c in fake.received if c.endswith("r?;")]
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_serial_sessions_never_overlap():
    """Name lookup and sweep share one port: the lock serialises them."""
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _hub_with_fake(fake)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.rollers_known.wait(), 3)
        await asyncio.wait_for(hub.serial_task, 3)
        await asyncio.gather(
            hub._serial_query_position("ABC"),
            hub._serial_query_position("ABC"),
            hub._serial_query_position("ABC"),
        )
        assert fake.connections == 4
        assert fake.max_concurrent == 1, "two serial sessions were open at once"
        assert hub.rollers["ABC"].signal == -92
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_requeue_against_a_silent_port_ends_and_the_sweep_resumes():
    """Reviewer finding: the hourly requeue used to retry forever and starve the sweep."""
    fake = FakeSerialHub(answer=False)
    hub = await _hub_with_fake(fake, signal_poll_interval=10)
    hub.signal_poll_interval = 0.05
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 5)
        assert hub.rollers["ABC"].name == "Shade ABC"
        assert fake.connections == devices.SERIAL_FALLBACK_ATTEMPTS

        hub.requeue_placeholder_names()  # the hourly tick, port still silent
        await asyncio.wait_for(hub.serial_task, 5)  # must END, not loop forever
        assert fake.connections == 2 * devices.SERIAL_FALLBACK_ATTEMPTS
        assert hub.unknown_rollers == set()
        assert hub.rollers["ABC"].name == "Shade ABC"

        hub.signal_task = asyncio.create_task(hub.signal_sweep())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(c.endswith("r?;") for c in fake.received):
                break
        assert "!ABCr?;" in fake.received, "the sweep stayed blocked"
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_a_bad_reply_does_not_end_the_name_lookup(monkeypatch):
    fake = FakeSerialHub(answer=False)
    hub = await _hub_with_fake(fake)
    calls = []

    async def boom(rollerids):
        calls.append(1)
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hub, "_serial_query_names", boom)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 5)
        assert len(calls) == devices.SERIAL_FALLBACK_ATTEMPTS
        assert hub.rollers["ABC"].name == "Shade ABC", "no roller is left nameless"
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_connect_timeout_releases_the_lock(monkeypatch):
    monkeypatch.setattr(devices, "SERIAL_CONNECT_TIMEOUT", 0.05)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(devices.asyncio, "open_connection", hang)
    hub = Hub("127.0.0.1", serial_port=1)
    hub.running = True
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
    await hub._cancel_background_tasks()  # stop the name runner; test the session alone
    with pytest.raises(asyncio.TimeoutError):
        await hub._serial_query_position("ABC")
    assert not hub.serial_lock.locked()


# --- missed signal checks ----------------------------------------------------------
# A check is a sweep query whose serial session opened; a miss is one that ended
# without the shade's reply. The hub's cached "rs" never stands in while the
# sweep is on, so a miss shows as no reading rather than as an old number.


async def _named_hub(fake, shades):
    hub = await _hub_with_fake(fake, signal_poll_interval=10)
    await hub.wsconsumer(shadow_message({rid: {"rs": -82} for rid in shades}))
    await asyncio.wait_for(hub.rollers_known.wait(), 3)
    await asyncio.wait_for(hub.serial_task, 3)
    return hub


async def test_a_silent_shade_counts_a_miss_and_reads_none():
    fake = FakeSerialHub(names={"ABC": "Kitchen"}, silent={"ABC"})
    hub = await _named_hub(fake, ["ABC"])
    roller = hub.rollers["ABC"]
    seen = []

    async def record(r):
        seen.append((r.signal_checks, r.signal_misses))

    roller.callback_subscribe(record)
    try:
        assert roller.signal_reading is None, "the cached rs is not a reading"
        await hub._serial_query_position("ABC")
        await asyncio.sleep(0)
        assert "!ABCr?;" in fake.received, "the query was sent"
        assert (roller.signal_checks, roller.signal_misses) == (1, 1)
        assert roller.signal_missed is True
        assert roller.signal_reading is None
        assert seen == [(1, 1)], "consumers are told the counts changed"
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_a_reply_counts_a_check_and_no_miss():
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _named_hub(fake, ["ABC"])
    roller = hub.rollers["ABC"]
    try:
        await hub._serial_query_position("ABC")
        assert (roller.signal_checks, roller.signal_misses) == (1, 0)
        assert roller.signal_missed is False
        assert roller.signal_reading == -92
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_an_unreachable_port_counts_neither_a_check_nor_a_miss(monkeypatch):
    """The port is the hub's problem; the shade is not blamed for it."""
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _named_hub(fake, ["ABC"])
    roller = hub.rollers["ABC"]
    try:
        await hub._serial_query_position("ABC")  # one good reading first
        assert (roller.signal_checks, roller.signal_misses) == (1, 0)
        await fake.stop()  # nothing listens on that port now: connection refused
        with pytest.raises(OSError):
            await hub._serial_query_position("ABC")
        assert (roller.signal_checks, roller.signal_misses) == (1, 0)
        assert roller.signal_missed is False

        # A connect timeout is the same story
        monkeypatch.setattr(devices, "SERIAL_CONNECT_TIMEOUT", 0.05)

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(devices.asyncio, "open_connection", hang)
        with pytest.raises(asyncio.TimeoutError):
            await hub._serial_query_position("ABC")
        assert (roller.signal_checks, roller.signal_misses) == (1, 0)
    finally:
        await hub._cancel_background_tasks()


async def test_signal_is_none_after_a_miss_and_back_on_the_next_reply():
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _named_hub(fake, ["ABC"])
    roller = hub.rollers["ABC"]
    try:
        await hub._serial_query_position("ABC")
        assert roller.signal_reading == -92
        fake.silent.add("ABC")
        await hub._serial_query_position("ABC")
        assert roller.signal_reading is None
        assert roller.signal == -92, "the last value is kept, just not reported"
        # The cached rs must not sneak in through the shadow while the shade is out
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        assert roller.signal_reading is None
        fake.silent.discard("ABC")
        await hub._serial_query_position("ABC")
        assert roller.signal_reading == -92
        assert roller.signal_missed is False
        assert (roller.signal_checks, roller.signal_misses) == (3, 1)
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()



async def test_query_signal_is_one_sweep_query_and_reports_the_reply():
    """What the integration's Refresh signal button calls: the sweep's query, on demand."""
    fake = FakeSerialHub(names={"ABC": "Kitchen", "DEF": "Office"}, silent={"DEF"})
    hub = await _hub_with_fake(fake, signal_poll_interval=30)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}, "DEF": {"rs": -70}}))
        await asyncio.wait_for(hub.rollers_known.wait(), 3)
        await asyncio.wait_for(hub.serial_task, 3)
        fake.received.clear()

        assert await hub.query_signal("ABC") is True
        assert fake.received == ["!ABCr?;"]
        assert hub.rollers["ABC"].signal_reading == -92
        assert hub.rollers["ABC"].signal_checks == 1 and hub.rollers["ABC"].signal_misses == 0

        assert await hub.query_signal("DEF") is False, "out of range: a miss, like a sweep check"
        assert fake.received == ["!ABCr?;", "!DEFr?;"]
        assert hub.rollers["DEF"].signal_reading is None
        assert hub.rollers["DEF"].signal_misses == 1

        with pytest.raises(KeyError):
            await hub.query_signal("ZZZ")
        await fake.stop()
        with pytest.raises(OSError):
            await hub.query_signal("ABC")
    finally:
        await hub._cancel_background_tasks()
        if fake.server.is_serving():
            await fake.stop()


async def test_query_signal_coalesces_rapid_callers():
    """Six callers at once for one shade: one session, one query, the same answer for all."""
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _hub_with_fake(fake, signal_poll_interval=30)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.serial_task, 3)
        fake.received.clear()
        results = await asyncio.gather(*(hub.query_signal("ABC") for _ in range(6)))
        assert results == [True] * 6
        assert fake.received == ["!ABCr?;"]
        assert fake.connections == 2  # the name lookup, then the one query
        assert hub.rollers["ABC"].signal_checks == 1
        assert hub._signal_queries == {}, "nothing left in flight"
        # A later call is a new query
        assert await hub.query_signal("ABC") is True
        assert fake.received == ["!ABCr?;", "!ABCr?;"]
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


class FakeClock:
    """A monotonic clock the library's sleeps advance, so no test actually waits."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def test_serial_sessions_are_spaced_whoever_asks(monkeypatch):
    """0.75 s from one session's close to the next one's open: queries, sweep and name lookup alike."""
    import types

    clock = FakeClock()
    monkeypatch.setattr(devices, "SERIAL_SESSION_SPACING", 0.75)
    monkeypatch.setattr(devices, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=time.time))
    monkeypatch.setattr(devices, "asyncio", types.SimpleNamespace(**{**vars(asyncio), "sleep": clock.sleep}))
    fake = FakeSerialHub(names={"ABC": "Kitchen", "DEF": "Office"})
    hub = await _hub_with_fake(fake, signal_poll_interval=30)
    try:
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}, "DEF": {"rs": -70}}))
        await asyncio.wait_for(hub.serial_task, 3)  # the name lookup: the first session
        assert fake.connections == 1
        assert clock.sleeps == [], "nothing to wait for before the first session"

        assert await hub.query_signal("ABC") is True
        assert clock.sleeps == [0.75], "a full spacing after the name lookup closed"
        clock.now += 0.5  # half the spacing passes on its own
        assert await hub.query_signal("DEF") is True
        assert clock.sleeps == [0.75, 0.25], "only the remainder is waited out"
        assert fake.connections == 3
        assert fake.received[-2:] == ["!ABCr?;", "!DEFr?;"]

        clock.now += 1  # more than the spacing has passed: no wait at all
        await hub._serial_query_position("ABC")  # what a sweep tick does
        assert clock.sleeps == [0.75, 0.25]
        assert fake.connections == 4
    finally:
        await hub._cancel_background_tasks()
        await fake.stop()


async def test_serial_sessions_and_open_failures_are_counted():
    """For diagnostics: how many sessions opened the port and how many could not."""
    fake = FakeSerialHub(names={"ABC": "Kitchen"})
    hub = await _hub_with_fake(fake)
    try:
        assert hub.serial_sessions == 0 and hub.serial_open_failures == 0
        await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
        await asyncio.wait_for(hub.rollers_known.wait(), 3)
        await asyncio.wait_for(hub.serial_task, 3)
        assert hub.serial_sessions == 1
        await hub.query_signal("ABC")
        assert hub.serial_sessions == 2
        assert hub.serial_open_failures == 0

        await fake.stop()  # nothing listens now
        with pytest.raises(OSError):
            await hub.query_signal("ABC")
        assert hub.serial_sessions == 2
        assert hub.serial_open_failures == 1
    finally:
        await hub._cancel_background_tasks()
