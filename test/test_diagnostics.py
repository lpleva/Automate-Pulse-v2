"""Download diagnostics: what the integration holds in memory, redacted, no network.

Every test goes through Home Assistant's own /api/diagnostics endpoint, so the
platform file is proven to load, and the result is proven to be JSON.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)

from custom_components.automate.const import DOMAIN
from custom_components.automate.vendor.aiopulse2 import devices

from .conftest import HUB_HOST, HUB_ID, HUB_MAC, feed, settle
from .test_integration import _setup, _sweep_entry

pytestmark = pytest.mark.usefixtures("no_network")

# Values that must never reach the file, wherever the hub put them.
SECRETS = {
    "ssid": "PlevaHome",
    "password": "wifi-secret-1",
    "jwt": "eyJhbGciOi.fake-jwt.sig",
    "timezone": "America/Chicago",
    "lat": 41.91,
    "lon": -88.31,
    "serialNumber": "SN-0001",
    "token": "shade-token-9",
}

KITCHEN = {
    "rs": -82,
    "ol": True,
    "mp": 30,
    "is": True,
    "vo": "12.1D23",
    "ls": True,  # limits set
    "ta": 0,  # tilt
    "somethingNew": {"the hub": "may send fields this integration never reads"},
    "token": SECRETS["token"],
}
OFFICE = {"rs": -70, "ol": True, "mp": 0, "is": True}

HUB_EXTRAS = {
    "onlineLatest": 1760300000,
    "wifi": {"ssid": SECRETS["ssid"], "password": SECRETS["password"]},
    "jwt": SECRETS["jwt"],
    "timezone": SECRETS["timezone"],
    "lat": SECRETS["lat"],
    "lon": SECRETS["lon"],
    "serialNumber": SECRETS["serialNumber"],
}


async def _two_shades(hass, hub):
    await feed(
        hass,
        hub,
        {"ABC": dict(KITCHEN), "DEF": dict(OFFICE)},
        names={"ABC": "Kitchen", "DEF": "Office"},
        **HUB_EXTRAS,
    )


async def test_config_entry_diagnostics_cover_the_hub_and_every_shade(hass, hass_client):
    entry = _sweep_entry(hass)
    hub = await _setup(hass, entry)
    await _two_shades(hass, hub)
    hub.api.response_parse("!ABCr050b000,R54;")  # one live reading for the kitchen
    await settle(hass)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert set(diag) == {"entry", "hub", "shades"}

    assert diag["entry"]["unique_id"] == HUB_ID
    assert diag["entry"]["data"] == {"host": HUB_HOST}
    assert diag["entry"]["options"] == {"signal_poll_interval": 30}

    hub_diag = diag["hub"]
    assert hub_diag["id"] == HUB_ID
    assert hub_diag["name"] == "Living room hub"
    assert hub_diag["host"] == HUB_HOST, "the LAN address is kept"
    assert hub_diag["mac"] == HUB_MAC, "the MAC is kept"
    assert hub_diag["model"] == "Pulse Pro Hub"
    assert hub_diag["firmware_version"] == "1.3.0"
    assert hub_diag["matter"] == {"vendorID": 4938, "productID": 1}
    assert hub_diag["online_latest"] == HUB_EXTRAS["onlineLatest"]
    assert hub_diag["library_version"] == "1.0.0+lpleva"
    assert hub_diag["connection"]["connected"] is True
    assert hub_diag["connection"]["websocket_open"] is False, "no websocket in the tests"
    assert hub_diag["polling"] == {
        "shadow_poll_interval_s": 10,
        "signal_poll_interval_s": 30,
        "live_sweep_on": True,
        "live_reading_stale_after_s": hub.api.signal_stale_after(),
        "sweep_next_index": 0,
        "pending_payloads": 1,  # the office shade has no "vo": its details query is queued
        "details_requested_for": ["DEF"],
    }
    assert hub_diag["serial_protocol"]["port"] == 1487
    assert hub_diag["serial_protocol"]["sessions_opened"] == 0
    assert hub_diag["serial_protocol"]["open_failures"] == 0
    assert hub_diag["serial_protocol"]["unnamed_shades"] == []
    assert hub_diag["shadow"]["hubId"] == HUB_ID
    assert "shades" not in hub_diag["shadow"], "the shades are reported per shade, not twice"
    assert hub_diag["shadow_age_s"] is not None and hub_diag["shadow_age_s"] < 5

    assert set(diag["shades"]) == {"ABC", "DEF"}
    kitchen = diag["shades"]["ABC"]
    assert kitchen["name"] == "Kitchen"
    assert kitchen["name_is_placeholder"] is False
    assert kitchen["entities"] == {
        "cover": "cover.kitchen",
        "battery": "sensor.kitchen_battery",
        "signal": "sensor.kitchen_signal",
        "missed_signal_checks": "sensor.kitchen_missed_signal_checks",
        "refresh_signal": "button.kitchen_refresh_signal",
    }
    assert kitchen["online"] is True
    assert kitchen["position"]["closed_percent"] == 50, "the live reading"
    assert kitchen["position"]["cover_position"] == 50
    assert kitchen["position"]["action"] == "stopped"
    assert kitchen["position"]["live_reading_in_use"] is True
    assert kitchen["signal"]["dbm"] == -92
    assert kitchen["signal"]["sensor_reading_dbm"] == -92
    assert kitchen["signal"]["live_reading_fresh"] is True
    assert kitchen["signal"]["live_reading_age_s"] < 5
    assert kitchen["signal"]["checks"] == 0 and kitchen["signal"]["misses"] == 0
    assert kitchen["battery"] == {"has_battery": True, "voltage": 12.1, "percent": 76}
    assert kitchen["motor"] == {
        "device_type_code": "D",
        "device_type": "DC motor",
        "firmware_version": "23",
        "vo": {
            "raw": "12.1D23",
            "decoded": True,
            "volts": 12.1,
            "device_type_code": "D",
            "device_type": "DC motor",
            "motor_firmware_version": "23",
        },
    }

    office = diag["shades"]["DEF"]
    assert office["entities"]["battery"] is None, "no vo yet, so no battery sensor"
    assert office["signal"]["dbm"] is None, "sweep on: the cached rs is never applied (1.2.0)"
    assert office["shadow"]["rs"] == -70, "but the shadow still shows what the hub cached"
    assert office["signal"]["sensor_reading_dbm"] is None, "sweep on, no live reading yet"
    assert office["signal"]["live_reading_age_s"] is None
    assert office["battery"] == {"has_battery": False, "voltage": None, "percent": None}
    assert office["motor"]["vo"] is None


async def test_the_shadow_passes_through_with_every_field_the_hub_sent(hass, hass_client, config_entry):
    hub = await _setup(hass, config_entry)
    await _two_shades(hass, hub)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    shadow = diag["shades"]["ABC"]["shadow"]
    assert shadow["ls"] is True
    assert shadow["ta"] == 0
    assert shadow["is"] is True
    assert shadow["somethingNew"] == KITCHEN["somethingNew"]
    assert set(shadow) == set(KITCHEN), "the whole entry, not just the keys the library reads"
    assert diag["shades"]["DEF"]["shadow"] == OFFICE
    assert diag["hub"]["shadow"]["onlineLatest"] == HUB_EXTRAS["onlineLatest"]
    assert diag["hub"]["shadow"]["firmware"] == {"version": "1.3.0", "RFversion": "1.0"}

    # A later poll replaces it; a poll that leaves a shade out leaves its copy alone
    await feed(hass, hub, {"ABC": {"rs": -80, "ol": True}})
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    assert diag["shades"]["ABC"]["shadow"] == {"rs": -80, "ol": True}
    assert diag["shades"]["DEF"]["shadow"] == OFFICE


async def test_sensitive_fields_are_redacted_wherever_they_appear(hass, hass_client, config_entry):
    hub = await _setup(hass, config_entry)
    await _two_shades(hass, hub)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    hub_shadow = diag["hub"]["shadow"]
    assert hub_shadow["wifi"] == REDACTED, "the whole wifi object, ssid and password inside it"
    assert hub_shadow["jwt"] == REDACTED
    assert hub_shadow["timezone"] == REDACTED
    assert hub_shadow["lat"] == REDACTED and hub_shadow["lon"] == REDACTED
    assert hub_shadow["serialNumber"] == REDACTED
    assert diag["shades"]["ABC"]["shadow"]["token"] == REDACTED
    # Kept on purpose: both are LAN-only and both are needed to tell hubs apart
    assert diag["hub"]["mac"] == HUB_MAC
    assert diag["hub"]["host"] == HUB_HOST
    assert hub_shadow["mac"] == HUB_MAC
    assert diag["hub"]["serial_protocol"]["port"] == 1487, "our own section is not caught by the serial key"

    text = json.dumps(diag)
    for key, value in SECRETS.items():
        assert str(value) not in text, f"{key} leaked"


async def test_diagnostics_make_no_network_calls(hass, hass_client, config_entry):
    """Sockets are off in the suite anyway; this pins it at the library's own exits."""
    hub = await _setup(hass, config_entry)
    await _two_shades(hass, hub)
    with (
        patch.object(devices.Hub, "_serial_session", new=AsyncMock()) as serial,
        patch.object(devices.Hub, "sendws", new=AsyncMock()) as sendws,
        patch.object(devices.Hub, "send_payload", new=AsyncMock()) as send_payload,
        patch.object(devices.asyncio, "open_connection", new=AsyncMock()) as open_connection,
        patch.object(devices.websockets, "connect") as ws_connect,
    ):
        await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
        dev_reg = dr.async_get(hass)
        for ident in (HUB_ID, "ABC"):
            device = dev_reg.async_get_device_by_identifier((DOMAIN, ident), config_entry.entry_id)
            await get_diagnostics_for_device(hass, hass_client, config_entry, device)
    for mock in (serial, sendws, send_payload, open_connection, ws_connect):
        mock.assert_not_called()


async def test_device_diagnostics_for_a_shade_and_for_the_hub(hass, hass_client, config_entry):
    hub = await _setup(hass, config_entry)
    await _two_shades(hass, hub)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    kitchen_device = dev_reg.async_get(ent_reg.async_get("cover.kitchen").device_id)
    diag = await get_diagnostics_for_device(hass, hass_client, config_entry, kitchen_device)
    assert set(diag) == {"entry", "hub", "shades"}
    assert set(diag["shades"]) == {"ABC"}, "that shade only"
    assert diag["shades"]["ABC"]["entities"]["cover"] == "cover.kitchen"
    assert diag["hub"]["id"] == HUB_ID
    assert diag["hub"]["shadow"]["jwt"] == REDACTED

    hub_device = dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id)
    diag = await get_diagnostics_for_device(hass, hass_client, config_entry, hub_device)
    assert diag["shades"] == {}
    assert diag["shade_ids"] == ["ABC", "DEF"]
    assert diag["hub"]["name"] == "Living room hub"


async def test_diagnostics_before_the_hub_has_reported(hass, hass_client, config_entry):
    """Set up, nothing received yet: no id, no shades, no shadow, and no error."""
    await _setup(hass, config_entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    assert diag["hub"]["id"] is None
    assert diag["hub"]["shadow"] is None
    assert diag["hub"]["shadow_age_s"] is None
    assert diag["hub"]["online_latest"] is None
    assert diag["hub"]["matter"] is None
    assert diag["hub"]["connection"]["connected"] is False
    assert diag["hub"]["polling"]["live_sweep_on"] is False
    assert diag["hub"]["polling"]["live_reading_stale_after_s"] is None
    assert diag["shades"] == {}
