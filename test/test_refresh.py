"""The Refresh button and the `automate.refresh` action: signal, position AND battery in one press.

Lukas's requirements of 2026-11-07, one test per requirement (R1..R7). The
websocket is the `no_network` stand-in, so every shadow write is caught at
Hub.sendws, the one place the library writes to its websocket; the serial
query goes to the fake hub on 127.0.0.1 as in test_integration.
"""

import asyncio
import json
import re
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.const import ATTR_VOLTAGE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.automate.const import CONF_POLL_INTERVAL, CONF_SIGNAL_POLL_INTERVAL, DOMAIN
from custom_components.automate.vendor.aiopulse2 import devices, errors

from .conftest import HUB_HOST, HUB_ID, feed, settle, shade_id, shadow_message
from .test_integration import _button_entity, _setup, _sweep_entry
from .test_library_serial import FakeClock, FakeSerialHub

pytestmark = pytest.mark.usefixtures("no_network", "socket_enabled")

# Three battery shades, `vo` present from the start (the case R2 is about: the
# voltage must refresh even though the hub's shadow already had one).
KITCHEN = {"rs": -82, "ol": True, "mp": 30, "vo": "12.1D23"}  # 76 %
OFFICE = {"rs": -70, "ol": True, "mp": 0, "vo": "7.9D23"}  # 83 % (the 8.3 V battery)
HALL = {"rs": -75, "ol": True, "mp": 100, "vo": "11.5D23"}  # 60 %
SHADES = {"ABC": KITCHEN, "DEF": OFFICE, "GHI": HALL}
NAMES = {"ABC": "Kitchen", "DEF": "Office", "GHI": "Hall"}


def _entry(hass, poll_interval=None):
    """A hub entry with the sweep on; `poll_interval` as the house runs it (30 s)."""
    options = {CONF_SIGNAL_POLL_INTERVAL: 30}
    if poll_interval is not None:
        options[CONF_POLL_INTERVAL] = poll_interval
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": HUB_HOST}, unique_id=HUB_ID, options=options, version=2
    )
    entry.add_to_hass(hass)
    return entry


async def _hub_with_shades(hass, fake, entry=None, shades=("ABC", "DEF")):
    """The integration set up against the fake serial hub, websocket "up", shades named."""
    hub = await _setup(hass, entry or _sweep_entry(hass))
    hub.api.host = "127.0.0.1"
    hub.api.serial_port = await fake.start()
    hub.api.handshake.set()  # the websocket is up: send_payload waits on this
    await feed(
        hass,
        hub,
        {rid: dict(SHADES[rid]) for rid in shades},
        names={rid: NAMES[rid] for rid in shades},
    )
    return hub


def _desired(sendws):
    """The `desired` object of every payload written to the websocket, in order."""
    return [call.args[0]["args"]["desired"] for call in sendws.await_args_list]


def _query(rid):
    return {"shades": {rid: {"query": True}}}


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await settle(hass)


# --- R1: the button is "Refresh"; unique id and entity id unchanged -----------------


async def test_r1_button_is_named_refresh_and_keeps_its_unique_id_and_entity_id(hass):
    """An existing install: the registry already holds button.kitchen_refresh_signal."""
    entry = _sweep_entry(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "button",
        DOMAIN,
        shade_id("ABC", suffix="_refresh_signal"),
        config_entry=entry,
        suggested_object_id="kitchen_refresh_signal",
    )
    hub = await _setup(hass, entry)
    await feed(hass, hub, {"ABC": dict(KITCHEN), "DEF": dict(OFFICE)}, names=NAMES)

    state = hass.states.get("button.kitchen_refresh_signal")
    assert state is not None, hass.states.async_entity_ids("button")
    assert state.attributes["friendly_name"] == "Kitchen Refresh"
    reg = ent_reg.async_get("button.kitchen_refresh_signal")
    assert reg.unique_id == shade_id("ABC", suffix="_refresh_signal"), "the unique id is what keeps the entity id"
    assert reg.original_name == "Refresh"
    assert reg.entity_category is er.EntityCategory.DIAGNOSTIC
    assert reg.device_id == ent_reg.async_get("cover.kitchen").device_id

    # A shade first seen by this release gets its entity id from the new name,
    # but the same unique id suffix, so the two installs are the same thing.
    fresh = ent_reg.async_get("button.office_refresh")
    assert fresh is not None, hass.states.async_entity_ids("button")
    assert fresh.unique_id == shade_id("DEF", suffix="_refresh_signal")
    assert hass.states.get("button.office_refresh").attributes["friendly_name"] == "Office Refresh"
    assert sorted(hass.states.async_entity_ids("button")) == [
        "button.kitchen_refresh_signal",
        "button.office_refresh",
    ], "one button per shade, no second copy"


# --- R2: one press refreshes signal, position and battery ---------------------------


async def test_r2_one_press_refreshes_signal_position_and_battery(hass):
    fake = FakeSerialHub()
    with patch.object(devices.Hub, "sendws", new=AsyncMock(return_value=True)) as sendws:
        hub = await _hub_with_shades(hass, fake)
        try:
            battery = hass.states.get("sensor.office_battery")
            assert battery.state == "83" and battery.attributes[ATTR_VOLTAGE] == 7.9
            assert hass.states.get("sensor.office_signal").state == STATE_UNKNOWN
            assert hass.states.get("cover.office").attributes["current_position"] == 100
            sendws.reset_mock()

            await _press(hass, "button.office_refresh")

            # The serial part: the sweep's query, and its reply (50 % closed, R54 = -92 dBm)
            # lands as signal and position right away.
            assert fake.received == ["!DEFr?;"]
            assert hass.states.get("sensor.office_signal").state == "-92"
            assert hass.states.get("cover.office").attributes["current_position"] == 50
            # The websocket part: the app's queryDevice key, for that shade only,
            # in the envelope the library's other shadow writes use.
            payloads = [call.args[0] for call in sendws.await_args_list]
            assert len(payloads) == 1, payloads
            assert payloads[0]["method"] == "shadow"
            assert payloads[0]["args"]["desired"] == _query("DEF")
            assert isinstance(payloads[0]["args"]["timeStamp"], float)
            assert set(payloads[0]["args"]) == {"desired", "timeStamp"}

            # The hub re-read the motor; the next shadow poll carries the new voltage,
            # and the battery sensor follows it although `vo` was there before.
            await feed(hass, hub, {"DEF": {**OFFICE, "vo": "7.7D23"}})
            battery = hass.states.get("sensor.office_battery")
            assert battery.state == "74"
            assert battery.attributes[ATTR_VOLTAGE] == 7.7
            kitchen = hass.states.get("sensor.kitchen_battery")
            assert kitchen.state == "76" and kitchen.attributes[ATTR_VOLTAGE] == 12.1, "the other shade untouched"
            assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN
        finally:
            await fake.stop()


# --- R3: `automate.refresh` does the same; `automate.refresh_signal` keeps working ---


async def test_r3_refresh_action_does_the_full_refresh_and_refresh_signal_stays_signal_only(hass):
    fake = FakeSerialHub()
    with patch.object(devices.Hub, "sendws", new=AsyncMock(return_value=True)) as sendws:
        hub = await _hub_with_shades(hass, fake)
        try:
            assert hass.services.has_service(DOMAIN, "refresh")
            assert hass.services.has_service(DOMAIN, "refresh_signal")
            sendws.reset_mock()

            # Targets in the order given, one refresh per shade however often it
            # is named, through any of its entities.
            await hass.services.async_call(
                DOMAIN,
                "refresh",
                {"entity_id": ["cover.office", "button.kitchen_refresh", "cover.kitchen"]},
                blocking=True,
            )
            await settle(hass)
            assert fake.received == ["!DEFr?;", "!ABCr?;"]
            assert _desired(sendws) == [_query("DEF"), _query("ABC")]
            assert hass.states.get("sensor.office_signal").state == "-92"
            assert hass.states.get("sensor.kitchen_signal").state == "-92"
            await feed(hass, hub, {"ABC": {**KITCHEN, "vo": "11.9D23"}})
            assert hass.states.get("sensor.kitchen_battery").attributes[ATTR_VOLTAGE] == 11.9

            # `automate.refresh_signal` is still the signal query alone: the serial
            # `r?` and nothing over the websocket.
            fake.received.clear()
            sendws.reset_mock()
            await hass.services.async_call(
                DOMAIN, "refresh_signal", {"entity_id": "cover.office"}, blocking=True
            )
            await settle(hass)
            assert fake.received == ["!DEFr?;"]
            sendws.assert_not_awaited()

            # No target at all is refused by the schema, as for refresh_signal.
            with pytest.raises(vol.Invalid):
                await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
        finally:
            await fake.stop()


# --- R4: errors name the shade and the part that failed ------------------------------


async def test_r4_errors_name_the_shade_and_the_part_that_failed(hass, monkeypatch, caplog):
    monkeypatch.setattr(devices, "SIGNAL_POLL_REPLY_TIMEOUT", 0.3)
    monkeypatch.setattr(devices, "SEND_HANDSHAKE_TIMEOUT", 0.1)
    fake = FakeSerialHub(silent={"ABC"})
    sendws = AsyncMock(return_value=True)
    with patch.object(devices.Hub, "sendws", new=sendws):
        hub = await _hub_with_shades(hass, fake, shades=("ABC", "DEF", "GHI"))
        try:
            kitchen = _button_entity(hass, "button.kitchen_refresh")
            office = _button_entity(hass, "button.office_refresh")
            hall = _button_entity(hass, "button.hall_refresh")
            sendws.reset_mock()

            # Hub disconnected: nothing is sent on either path.
            hub.api.connected = False
            with pytest.raises(HomeAssistantError, match="Kitchen.*hub.*not connected"):
                await kitchen.async_press()
            assert fake.received == [] and sendws.await_args_list == []
            hub.api.connected = True

            # Shade offline (the hub says so): nothing is sent.
            await feed(hass, hub, {"ABC": {"ol": False}})
            with pytest.raises(HomeAssistantError, match="Kitchen.*offline"):
                await kitchen.async_press()
            assert fake.received == [] and sendws.await_args_list == []
            await feed(hass, hub, {"ABC": {"ol": True}})

            # Shade out of radio range: the signal query got no answer, the battery
            # query still went out, and the message says both.
            with pytest.raises(HomeAssistantError) as err:
                await kitchen.async_press()
            message = str(err.value)
            assert "Kitchen" in message
            assert "did not answer the signal query" in message
            assert "battery query was sent" in message
            assert fake.received == ["!ABCr?;"]
            assert _desired(sendws) == [_query("ABC")]

            # Websocket send fails while the serial port works: the reading lands,
            # the battery part is named, and the part that worked is named too.
            fake.received.clear()
            sendws.reset_mock()
            sendws.return_value = False
            with pytest.raises(HomeAssistantError) as err:
                await office.async_press()
            await settle(hass)
            message = str(err.value)
            assert "Office" in message
            assert "battery query could not be sent" in message and "websocket" in message
            assert "signal query was answered" in message
            assert fake.received == ["!DEFr?;"]
            assert hass.states.get("sensor.office_signal").state == "-92"

            # Websocket not connected at all (no handshake): same message, no 10 s wait.
            sendws.return_value = True
            hub.api.handshake.clear()
            fake.received.clear()
            with pytest.raises(HomeAssistantError, match="Office.*battery query could not be sent.*websocket"):
                await office.async_press()
            assert fake.received == ["!DEFr?;"]
            hub.api.handshake.set()

            # Serial port dead, websocket fine: the serial part is named, the battery
            # query still went out.
            await fake.stop()
            sendws.reset_mock()
            with pytest.raises(HomeAssistantError) as err:
                await office.async_press()
            message = str(err.value)
            assert "Office" in message
            assert "signal query could not be sent" in message and "serial port" in message
            assert "battery query was sent" in message
            assert _desired(sendws) == [_query("DEF")]

            # Both dead: both parts named.
            sendws.reset_mock()
            sendws.return_value = False
            with pytest.raises(HomeAssistantError) as err:
                await hall.async_press()
            message = str(err.value)
            assert "Hall" in message
            assert "signal query could not be sent" in message and "serial port" in message
            assert "battery query could not be sent" in message and "websocket" in message

            # The hub has forgotten the shade (removed in the Pulse app).
            hub.api.rollers.pop("GHI")
            with pytest.raises(HomeAssistantError, match="Hall.*no longer known to the hub"):
                await hall.async_press()

            # The action logs the same message per shade and carries on.
            sendws.return_value = True
            caplog.clear()
            await hass.services.async_call(
                DOMAIN, "refresh", {"entity_id": ["cover.kitchen", "cover.office"]}, blocking=True
            )
            assert "cover.kitchen: Cannot" in caplog.text and "Kitchen" in caplog.text
            assert "cover.office: Cannot" in caplog.text and "Office" in caplog.text
            assert "serial port" in caplog.text
        finally:
            if fake.server.is_serving():
                await fake.stop()


# --- R5: the existing connection and locks; rapid presses share one refresh ----------


async def test_r5_rapid_presses_share_one_serial_and_one_websocket_query(hass, monkeypatch):
    """Six presses at once: one serial session, one `query`; a press within the spacing
    repeats the serial query (its own pending window is over) but not the websocket one."""
    clock = FakeClock()
    monkeypatch.setattr(
        devices, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=time.time)
    )
    fake = FakeSerialHub()
    with (
        patch.object(devices.Hub, "sendws", new=AsyncMock(return_value=True)) as sendws,
        patch.object(devices.websockets, "connect") as ws_connect,
    ):
        hub = await _hub_with_shades(hass, fake, entry=_entry(hass, poll_interval=30))
        try:
            assert hub.api.details_query_spacing() == 30, "the hub's poll interval"
            sendws.reset_mock()
            connections = fake.connections

            await asyncio.gather(*(_press(hass, "button.office_refresh") for _ in range(6)))
            await settle(hass)
            assert fake.received == ["!DEFr?;"]
            assert fake.connections == connections + 1, "one serial session, under the library's lock"
            assert _desired(sendws) == [_query("DEF")]
            ws_connect.assert_not_called(), "no second websocket"
            assert hass.states.get("sensor.office_missed_signal_checks").attributes["checks"] == 1

            # 10 s on: a new serial query (nothing pending any more), the same battery query shared
            clock.now += 10
            await _press(hass, "button.office_refresh")
            assert fake.received == ["!DEFr?;", "!DEFr?;"]
            assert _desired(sendws) == [_query("DEF")]

            # Another shade is not held back by the first one's query
            await _press(hass, "button.kitchen_refresh")
            assert _desired(sendws) == [_query("DEF"), _query("ABC")]

            # Past the spacing: a new battery query for the office
            clock.now += 21
            await _press(hass, "button.office_refresh")
            assert _desired(sendws) == [_query("DEF"), _query("ABC"), _query("DEF")]
            assert fake.received == ["!DEFr?;", "!DEFr?;", "!ABCr?;", "!DEFr?;"]
        finally:
            await fake.stop()


async def test_r5_library_query_details_uses_send_payload_and_is_spaced_per_shade(monkeypatch):
    """The library half: the payload goes through send_payload (the one websocket, the
    handshake wait, the send timeout); one `query` per shade per spacing; a send that
    failed is not counted as sent."""
    clock = FakeClock()
    monkeypatch.setattr(
        devices, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=time.time)
    )
    hub = devices.Hub(HUB_HOST, delay_callbacks=False, poll_interval=30)
    hub.running = True
    hub.handshake.set()
    sent = []

    async def fake_sendws(payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(hub, "sendws", fake_sendws)
    await hub.wsconsumer(shadow_message({"ABC": KITCHEN, "DEF": OFFICE}))

    assert hub.details_query_spacing() == 30
    assert devices.Hub(HUB_HOST, poll_interval=2).details_query_spacing() == 10, "floor for a fast poll"
    assert devices.Hub(HUB_HOST, poll_interval=300).details_query_spacing() == 300

    assert await hub.query_details("ABC") is True
    assert await hub.query_details("ABC") is False, "shared with the one just sent"
    assert await hub.query_details("DEF") is True, "spaced per shade, not per hub"
    assert [p["args"]["desired"] for p in sent] == [_query("ABC"), _query("DEF")]
    assert all(p["method"] == "shadow" and set(p["args"]) == {"desired", "timeStamp"} for p in sent)
    clock.now += 29
    assert await hub.query_details("ABC") is False
    clock.now += 1
    assert await hub.query_details("ABC") is True
    assert len(sent) == 3

    with pytest.raises(KeyError):
        await hub.query_details("ZZZ")

    # A send that failed is not recorded: the next call sends.
    clock.now += 60
    monkeypatch.setattr(hub, "sendws", AsyncMock(return_value=False))
    with pytest.raises(errors.NotConnectedException):
        await hub.query_details("ABC")
    monkeypatch.setattr(hub, "sendws", fake_sendws)
    assert await hub.query_details("ABC") is True
    assert len(sent) == 4

    clock.now += 60
    hub.running = False
    with pytest.raises(errors.NotRunningException):
        await hub.query_details("DEF")
    assert len(sent) == 4


# --- R6: read queries only ------------------------------------------------------------


async def test_r6_refresh_sends_only_read_queries(hass):
    """Every shade refreshed: `r?` on the serial port and `query` over the websocket,
    nothing that moves a shade or touches its limits."""
    fake = FakeSerialHub()
    with patch.object(devices.Hub, "sendws", new=AsyncMock(return_value=True)) as sendws:
        hub = await _hub_with_shades(hass, fake, shades=("ABC", "DEF", "GHI"))
        try:
            sendws.reset_mock()
            await hass.services.async_call(DOMAIN, "refresh", {"entity_id": "all"}, blocking=True)
            await settle(hass)

            assert sorted(fake.received) == ["!ABCr?;", "!DEFr?;", "!GHIr?;"]
            assert all(re.fullmatch(r"!\w{3}r\?;", cmd) for cmd in fake.received)
            payloads = [call.args[0] for call in sendws.await_args_list]
            assert len(payloads) == 3
            for payload in payloads:
                assert payload["method"] == "shadow"
                assert set(payload["args"]) == {"desired", "timeStamp"}
                assert set(payload["args"]["desired"]) == {"shades"}
                for rid, keys in payload["args"]["desired"]["shades"].items():
                    assert rid in SHADES
                    assert keys == {"query": True}, f"{rid}: only the read key"
            assert sorted(next(iter(p["args"]["desired"]["shades"])) for p in payloads) == ["ABC", "DEF", "GHI"]
            for roller in hub.api.rollers.values():
                assert roller.moving is False and roller.target_closed_percent in (None, roller.closed_percent)
        finally:
            await fake.stop()


# --- R7: the action is described where Home Assistant and people read -----------------


def test_r7_refresh_is_described_in_services_yaml_the_translations_and_the_docs():
    root = Path(__file__).resolve().parents[1]
    component = root / "custom_components" / "automate"

    services = yaml.safe_load((component / "services.yaml").read_text())
    assert set(services) == {"refresh", "refresh_signal"}
    for name in ("refresh", "refresh_signal"):
        target = services[name]["target"]["entity"]
        assert target["integration"] == "automate"
        assert set(target["domain"]) == {"cover", "button"}

    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((component / name).read_text())["services"]
        assert strings["refresh"]["name"] == "Refresh"
        description = strings["refresh"]["description"].lower()
        assert "signal" in description and "position" in description and "battery" in description
        assert "refresh signal button" not in strings["refresh_signal"]["description"].lower(), (
            "the button is called Refresh now"
        )
        assert "signal" in strings["refresh_signal"]["description"].lower()

    readme = (root / "README.md").read_text()
    assert "`automate.refresh`" in readme and "`automate.refresh_signal`" in readme
    changelog = (root / "CHANGELOG.md").read_text()
    unreleased = changelog.split("## Unreleased", 1)[1].split("\n## ", 1)[0]
    assert "`automate.refresh`" in unreleased
    assert "What this changes in the house" in unreleased
