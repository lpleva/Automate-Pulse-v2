"""The integration on Home Assistant 2026.9: config flow, entities, devices, options."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.automate.const import (
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.automate.vendor.aiopulse2 import errors

from .conftest import HUB_HOST, HUB_ID, feed, settle

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


async def test_live_sweep_option_keeps_the_serial_reading(hass):
    """Sweep on (opt-in): the live reading wins over the hub's cached rs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": HUB_HOST},
        unique_id=HUB_ID,
        options={CONF_SIGNAL_POLL_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    hub = await _setup(hass, entry)
    assert hub.api.signal_poll_interval == 30
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    hub.api.response_parse("!ABCr050b000,R54;")
    await settle(hass)
    assert hass.states.get("sensor.kitchen_signal").state == "-92"
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 50}})
    assert hass.states.get("sensor.kitchen_signal").state == "-92"


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
