"""The integration on Home Assistant 2026.9: config flow, entities, devices, options."""

import asyncio
from unittest.mock import patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from custom_components.automate.const import (
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.automate.vendor.aiopulse2 import devices, errors

from .conftest import HUB_HOST, HUB_ID, feed, settle
from .test_library_serial import FakeSerialHub

pytestmark = pytest.mark.usefixtures("no_network")


async def _setup(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][config_entry.entry_id]


# --- config flow ------------------------------------------------------------------


class FakeTestHub:
    """Stand-in for aiopulse2.Hub in the config flow (only .test() is used)."""

    outcome = None

    def __init__(self, host, **kwargs):
        self.host = host
        self.id = None
        self.name = None

    async def test(self):
        if isinstance(FakeTestHub.outcome, Exception):
            raise FakeTestHub.outcome
        self.id, self.name = FakeTestHub.outcome
        return True


@pytest.fixture
def fake_flow_hub():
    with patch("custom_components.automate.config_flow.aiopulse2.Hub", FakeTestHub):
        yield FakeTestHub


async def test_flow_creates_entry_with_hub_id_as_unique_id(hass, fake_flow_hub):
    fake_flow_hub.outcome = (HUB_ID, "Living room hub")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.automate.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": f" {HUB_HOST} "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room hub"
    assert result["data"] == {"host": HUB_HOST}
    assert result["result"].unique_id == HUB_ID


async def test_flow_aborts_when_the_hub_is_already_configured(hass, fake_flow_hub, config_entry):
    fake_flow_hub.outcome = (HUB_ID, "Living room hub")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.11"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data["host"] == "192.0.2.11", "a moved hub updates the host"


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (OSError("no route"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (errors.InvalidResponseException(), "cannot_connect"),
        (ValueError("bug"), "unknown"),
    ],
)
async def test_flow_errors(hass, fake_flow_hub, exc, error):
    fake_flow_hub.outcome = exc
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HUB_HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_flow_title_falls_back_to_host_when_hub_has_no_name(hass, fake_flow_hub):
    fake_flow_hub.outcome = (HUB_ID, None)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch("custom_components.automate.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HUB_HOST})
    assert result["title"] == f"Pulse Hub ({HUB_HOST})"


# --- entities ---------------------------------------------------------------------


async def test_entities_and_devices(hass, config_entry):
    hub = await _setup(hass, config_entry)
    assert hub.api.heartbeatinterval == 10
    assert hub.api.signal_poll_interval == 0, "the live sweep is opt-in"

    await feed(
        hass,
        hub,
        {"ABC": {"rs": -82, "ol": True, "mp": 30, "is": True, "vo": "12.1D23"}},
        names={"ABC": "Kitchen"},
    )

    signal = hass.states.get("sensor.kitchen_signal")
    assert signal is not None, hass.states.async_entity_ids()
    assert signal.state == "-82"
    assert signal.attributes["unit_of_measurement"] == "dBm"
    assert signal.attributes["device_class"] == "signal_strength"
    assert signal.attributes["friendly_name"] == "Kitchen Signal"

    battery = hass.states.get("sensor.kitchen_battery")
    assert battery.state == "76"  # int(27.4 * 12.1 - 255)
    assert battery.attributes["voltage"] == 12.1

    assert hass.states.get("sensor.kitchen_missed_signal_checks") is None, (
        "no sweep, no checks to miss: the counter sensor is only created with the sweep on"
    )

    cover = hass.states.get("cover.kitchen")
    assert cover.state == "open"
    assert cover.attributes["current_position"] == 70

    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("sensor.kitchen_signal").unique_id == "ABC_signal"
    assert ent_reg.async_get("sensor.kitchen_signal").disabled is False
    assert ent_reg.async_get("sensor.kitchen_battery").unique_id == "ABC_battery"
    assert ent_reg.async_get("cover.kitchen").unique_id == "ABC"

    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id)
    assert hub_device.model == "Pulse Pro Hub"
    assert hub_device.sw_version == "1.3.0"
    assert (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff") in hub_device.connections
    roller_device = dev_reg.async_get_device_by_identifier((DOMAIN, "ABC"), config_entry.entry_id)
    assert roller_device.name == "Kitchen"
    assert roller_device.via_device_id == hub_device.id
    assert roller_device.model == "DC motor"
    assert roller_device.sw_version == "23"

    assert config_entry.title == f"Living room hub ({HUB_HOST})"


async def test_signal_sensor_stays_in_dbm_after_a_serial_reading(hass, config_entry):
    """Sweep off (the default): the serial value is converted, and the cached rs still applies."""
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    assert hass.states.get("sensor.kitchen_signal").state == "-82"
    hub.api.response_parse("!ABCr050b000,R54;")
    await settle(hass)
    assert hass.states.get("sensor.kitchen_signal").state == "-92"  # upstream showed 84
    assert hass.states.get("cover.kitchen").attributes["current_position"] == 50
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 50}})
    assert hass.states.get("sensor.kitchen_signal").state == "-82"


def _sweep_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": HUB_HOST},
        unique_id=HUB_ID,
        options={CONF_SIGNAL_POLL_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    return entry


async def test_live_sweep_option_keeps_the_serial_reading(hass):
    """Sweep on (opt-in): the live reading is the value; the cached rs never is."""
    hub = await _setup(hass, _sweep_entry(hass))
    assert hub.api.signal_poll_interval == 30
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    # Before the sweep has reached the shade there is no current reading
    assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN
    misses = hass.states.get("sensor.kitchen_missed_signal_checks")
    assert misses is not None, hass.states.async_entity_ids()
    assert misses.state == "0"
    assert misses.attributes["checks"] == 0
    assert misses.attributes["state_class"] == "total_increasing"
    assert misses.attributes["friendly_name"] == "Kitchen Missed signal checks"
    assert "unit_of_measurement" not in misses.attributes
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("sensor.kitchen_missed_signal_checks").unique_id == "ABC_signal_misses"
    assert ent_reg.async_get("sensor.kitchen_missed_signal_checks").entity_category is er.EntityCategory.DIAGNOSTIC

    hub.api.response_parse("!ABCr050b000,R54;")
    await settle(hass)
    assert hass.states.get("sensor.kitchen_signal").state == "-92"
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 50}})
    assert hass.states.get("sensor.kitchen_signal").state == "-92"

    # The reading goes stale (the serial port has been down): unknown, not the cached rs.
    # The sweep tick is what notices; a hub poll alone does not re-report the sensor.
    hub.api.rollers["ABC"].serial_signal_at -= hub.api.signal_stale_after() + 1
    hub.api.expire_stale_signal_readings()
    await settle(hass)
    assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 50}})
    assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN
    hub.api.response_parse("!ABCr050b000,R54;")  # the port is back
    await settle(hass)
    assert hass.states.get("sensor.kitchen_signal").state == "-92"


@pytest.mark.usefixtures("socket_enabled")
async def test_a_missed_check_shows_in_home_assistant(hass, monkeypatch):
    """A real miss through the fake serial port: the signal reads unknown, the counter moves."""
    monkeypatch.setattr(devices, "SIGNAL_POLL_REPLY_TIMEOUT", 0.3)
    fake = FakeSerialHub(silent={"ABC"})
    hub = await _setup(hass, _sweep_entry(hass))
    hub.api.host = "127.0.0.1"  # the serial session dials hub.host; the fake is local
    hub.api.serial_port = await fake.start()
    try:
        await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
        await hub.api._serial_query_position("ABC")  # what one sweep tick does
        await settle(hass)
        assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN
        misses = hass.states.get("sensor.kitchen_missed_signal_checks")
        assert misses.state == "1"
        assert misses.attributes["checks"] == 1

        fake.silent.clear()
        await hub.api._serial_query_position("ABC")
        await settle(hass)
        assert hass.states.get("sensor.kitchen_signal").state == "-92"
        misses = hass.states.get("sensor.kitchen_missed_signal_checks")
        assert misses.state == "1"
        assert misses.attributes["checks"] == 2
    finally:
        await fake.stop()


async def test_battery_sensor_appears_once_the_motor_type_is_known(hass, config_entry):
    """Upstream never created it if the first update lacked "vo" (needed a restart)."""
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    assert hass.states.get("sensor.kitchen_signal") is not None
    assert hass.states.get("sensor.kitchen_battery") is None
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30, "vo": "8.1D23"}})
    battery = hass.states.get("sensor.kitchen_battery")
    assert battery is not None
    assert battery.state == "91"  # int(42.8 * 8.1 - 255)


async def test_ac_motor_gets_no_battery_sensor(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30, "vo": "24.0A12"}}, names={"ABC": "Patio"})
    assert hass.states.get("sensor.patio_signal") is not None
    assert hass.states.get("sensor.patio_battery") is None


async def test_unavailable_when_hub_disconnects_or_shade_offline(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    assert hass.states.get("sensor.kitchen_signal").state == "-82"
    await feed(hass, hub, {"ABC": {"ol": False}})
    assert hass.states.get("sensor.kitchen_signal").state == STATE_UNAVAILABLE
    await feed(hass, hub, {"ABC": {"ol": True}})
    assert hass.states.get("sensor.kitchen_signal").state == "-82"
    hub.api.connected = False
    hub.api.notify_callback()
    await settle(hass)
    assert hass.states.get("cover.kitchen").state == STATE_UNAVAILABLE


async def test_sparse_update_does_not_flap_entities(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    await feed(hass, hub, {"ABC": {"rs": -81}})
    cover = hass.states.get("cover.kitchen")
    assert cover.state == "open" and cover.attributes["current_position"] == 70
    assert hass.states.get("sensor.kitchen_signal").state == "-81"


async def test_options_are_passed_to_the_library_and_reload(hass, config_entry):
    hub = await _setup(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_SIGNAL_POLL_INTERVAL: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options == {CONF_POLL_INTERVAL: 60, CONF_SIGNAL_POLL_INTERVAL: 30}
    new_hub = hass.data[DOMAIN][config_entry.entry_id]
    assert new_hub is not hub, "the entry reloaded"
    assert new_hub.api.heartbeatinterval == 60
    assert new_hub.api.signal_poll_interval == 30

    # Below the library's floor (but not 0) is refused by the schema
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_SIGNAL_POLL_INTERVAL: 5}
        )
    hass.config_entries.options.async_abort(result["flow_id"])

    # And back off again, no code change: 0 means the cached hub value is used
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_SIGNAL_POLL_INTERVAL: 0}
    )
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][config_entry.entry_id].api.signal_poll_interval == 0


async def test_unload_stops_the_library(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    api = hub.api
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert api.running is False
    assert hub.api is None and hub.tasks == []
    assert config_entry.entry_id not in hass.data[DOMAIN]


# --- refresh signal: the button and the action ------------------------------------


async def _two_shades(hass, hub):
    await feed(
        hass,
        hub,
        {"ABC": {"rs": -82, "ol": True, "mp": 30}, "DEF": {"rs": -70, "ol": True, "mp": 0}},
        names={"ABC": "Kitchen", "DEF": "Office"},
    )


def _button_entity(hass, entity_id):
    """The live ButtonEntity object, for pressing it directly."""
    from homeassistant.helpers.entity_component import DATA_INSTANCES

    return hass.data[DATA_INSTANCES]["button"].get_entity(entity_id)


async def test_refresh_signal_button_per_shade(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await _two_shades(hass, hub)

    ent_reg = er.async_get(hass)
    for entity_id, roller_id, cover in (
        ("button.kitchen_refresh_signal", "ABC", "cover.kitchen"),
        ("button.office_refresh_signal", "DEF", "cover.office"),
    ):
        state = hass.states.get(entity_id)
        assert state is not None, hass.states.async_entity_ids()
        assert state.attributes["friendly_name"].endswith("Refresh signal")
        entry = ent_reg.async_get(entity_id)
        assert entry.unique_id == f"{roller_id}_refresh_signal"
        assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
        assert entry.device_id == ent_reg.async_get(cover).device_id, "same device as the shade"
    assert hass.services.has_service(DOMAIN, "refresh_signal")


@pytest.mark.usefixtures("socket_enabled")
async def test_pressing_the_button_sends_only_that_shades_query(hass):
    """One press, one `!<id>r?;` for that shade, nothing else; the sensor follows the reply."""
    fake = FakeSerialHub()
    hub = await _setup(hass, _sweep_entry(hass))
    hub.api.host = "127.0.0.1"
    hub.api.serial_port = await fake.start()
    try:
        await _two_shades(hass, hub)
        assert hass.states.get("sensor.office_signal").state == STATE_UNKNOWN

        await hass.services.async_call(
            "button", "press", {"entity_id": "button.office_refresh_signal"}, blocking=True
        )
        await settle(hass)

        assert fake.received == ["!DEFr?;"], "the sweep's query for that shade, and only that"
        assert fake.connections == 1
        assert hass.states.get("sensor.office_signal").state == "-92"
        assert hass.states.get("sensor.kitchen_signal").state == STATE_UNKNOWN, "the other shade untouched"
        misses = hass.states.get("sensor.office_missed_signal_checks")
        assert misses.state == "0" and misses.attributes["checks"] == 1
        assert hass.states.get("button.office_refresh_signal").state != STATE_UNKNOWN, "pressed"

        # Six rapid presses: one session, one query; every press completes
        fake.received.clear()
        await asyncio.gather(
            *(
                hass.services.async_call(
                    "button", "press", {"entity_id": "button.office_refresh_signal"}, blocking=True
                )
                for _ in range(6)
            )
        )
        await settle(hass)
        assert fake.received == ["!DEFr?;"]
        assert fake.connections == 2
        assert hass.states.get("sensor.office_missed_signal_checks").attributes["checks"] == 2
    finally:
        await fake.stop()


@pytest.mark.usefixtures("socket_enabled")
async def test_button_errors_are_clear(hass, monkeypatch, caplog):
    """A disconnected hub, an offline shade, a dead port and a silent shade each say so."""
    from homeassistant.exceptions import HomeAssistantError

    monkeypatch.setattr(devices, "SIGNAL_POLL_REPLY_TIMEOUT", 0.3)
    fake = FakeSerialHub(silent={"ABC"})
    hub = await _setup(hass, _sweep_entry(hass))
    hub.api.host = "127.0.0.1"
    port = await fake.start()
    hub.api.serial_port = port
    try:
        await _two_shades(hass, hub)
        button = _button_entity(hass, "button.kitchen_refresh_signal")

        # Hub disconnected: the button is unavailable, so HA's button.press skips it
        # (its own warning), and a direct press says why.
        hub.api.connected = False
        hub.api.notify_callback()
        await settle(hass)
        assert hass.states.get("button.kitchen_refresh_signal").state == STATE_UNAVAILABLE
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.kitchen_refresh_signal"}, blocking=True
        )
        with pytest.raises(HomeAssistantError, match="not connected"):
            await button.async_press()
        assert fake.received == []
        hub.api.connected = True

        # Shade offline
        await feed(hass, hub, {"ABC": {"ol": False}})
        with pytest.raises(HomeAssistantError, match="offline"):
            await button.async_press()
        assert fake.received == []
        await feed(hass, hub, {"ABC": {"ol": True}})

        # Serial port unreachable (nothing listens on this port)
        await fake.stop()
        with pytest.raises(HomeAssistantError, match="serial port"):
            await button.async_press()

        # Port back, but the shade is out of radio range: no reply
        hub.api.serial_port = await fake.start()
        with pytest.raises(HomeAssistantError, match="did not answer"):
            await button.async_press()
        assert fake.received == ["!ABCr?;"]
        await settle(hass)
        assert hass.states.get("sensor.kitchen_missed_signal_checks").state == "1"

        # The hub has forgotten the shade (it was removed from the hub's app)
        hub.api.rollers.pop("ABC")
        with pytest.raises(HomeAssistantError, match="no longer known to the hub"):
            await button.async_press()
    finally:
        await fake.stop()


@pytest.mark.usefixtures("socket_enabled")
async def test_refresh_signal_action_queries_each_target_in_order(hass, caplog):
    """Targets in the order given, one query per shade, unknown entities skipped with a warning."""
    fake = FakeSerialHub()
    hub = await _setup(hass, _sweep_entry(hass))
    hub.api.host = "127.0.0.1"
    hub.api.serial_port = await fake.start()
    try:
        await _two_shades(hass, hub)
        await hass.services.async_call(
            DOMAIN,
            "refresh_signal",
            {
                "entity_id": [
                    "cover.office",
                    "button.kitchen_refresh_signal",
                    "cover.kitchen",  # the same shade again: queried once
                    "cover.not_a_shade",
                    "light.kitchen",
                ]
            },
            blocking=True,
        )
        await settle(hass)
        assert fake.received == ["!DEFr?;", "!ABCr?;"]
        assert hass.states.get("sensor.office_signal").state == "-92"
        assert hass.states.get("sensor.kitchen_signal").state == "-92"
        assert "cover.not_a_shade is not one of this integration's shade entities" in caplog.text
        assert "light.kitchen is not one of this integration's shade entities" in caplog.text

        # A device target resolves to that shade's entities, still one query
        fake.received.clear()
        device_id = er.async_get(hass).async_get("cover.kitchen").device_id
        await hass.services.async_call(
            DOMAIN, "refresh_signal", {"device_id": device_id}, blocking=True
        )
        assert fake.received == ["!ABCr?;"]

        # `all`: every shade
        fake.received.clear()
        await hass.services.async_call(DOMAIN, "refresh_signal", {"entity_id": "all"}, blocking=True)
        assert fake.received == ["!ABCr?;", "!DEFr?;"]

        # No target at all is refused by the schema
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "refresh_signal", {}, blocking=True)
    finally:
        await fake.stop()


async def test_refresh_signal_action_warns_per_shade_when_the_hub_is_disconnected(hass, caplog):
    hub = await _setup(hass, _sweep_entry(hass))
    await _two_shades(hass, hub)
    hub.api.connected = False
    with patch.object(devices.Hub, "_serial_session") as session:
        await hass.services.async_call(
            DOMAIN, "refresh_signal", {"entity_id": ["cover.kitchen", "cover.office"]}, blocking=True
        )
    session.assert_not_called()
    assert "cover.kitchen: Cannot refresh the signal of Kitchen" in caplog.text
    assert "cover.office: Cannot refresh the signal of Office" in caplog.text
    assert "not connected" in caplog.text


@pytest.mark.usefixtures("socket_enabled")
async def test_refresh_signal_action_area_target_skips_other_entities_silently(hass, caplog):
    """An area with a shade, a light and a lock: the shade is queried, nothing is warned about."""
    fake = FakeSerialHub()
    hub = await _setup(hass, _sweep_entry(hass))
    hub.api.host = "127.0.0.1"
    hub.api.serial_port = await fake.start()
    try:
        await _two_shades(hass, hub)
        area = ar.async_get(hass).async_create("Kitchen")
        ent_reg = er.async_get(hass)
        dr.async_get(hass).async_update_device(
            ent_reg.async_get("cover.kitchen").device_id, area_id=area.id
        )
        for domain, platform in (("light", "demo"), ("lock", "demo")):
            entry = ent_reg.async_get_or_create(domain, platform, f"{domain}-1", suggested_object_id="kitchen")
            ent_reg.async_update_entity(entry.entity_id, area_id=area.id)
            hass.states.async_set(entry.entity_id, "off")

        caplog.clear()
        await hass.services.async_call(DOMAIN, "refresh_signal", {"area_id": area.id}, blocking=True)
        await settle(hass)
        assert fake.received == ["!ABCr?;"]
        assert hass.states.get("sensor.kitchen_signal").state == "-92"
        assert "not one of this integration" not in caplog.text
        assert "missing or not currently available" not in caplog.text
        assert "WARNING" not in caplog.text, caplog.text
    finally:
        await fake.stop()
