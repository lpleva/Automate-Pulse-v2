"""Deleting a stale device from Settings > Devices (async_remove_config_entry_device)."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.automate import async_remove_config_entry_device
from custom_components.automate.const import DOMAIN

from .conftest import HUB_HOST, HUB_ID, feed

pytestmark = pytest.mark.usefixtures("no_network")

OTHER_HUB_HOST = "192.0.2.11"
OTHER_HUB_ID = "2009999"


async def _setup(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][config_entry.entry_id]


def _stale_device(hass, config_entry, roller_id, name):
    """A shade's device as the registry restores it after a restart: the hub never
    lists the motor again, so no entity is ever created for it and nothing removes it."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, roller_id)},
        manufacturer="Rollease Acmeda",
        name=name,
    )


async def _ws_remove(hass, hass_ws_client, device_id):
    """Drive the real path: the frontend's Delete button, via the websocket command."""
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device_id}
    )
    return await client.receive_json()


REJECTED = "Failed to remove device entry, rejected by integration"


async def test_a_live_shade_cannot_be_deleted(hass, hass_ws_client, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier((DOMAIN, "ABC"), config_entry.entry_id)

    assert await async_remove_config_entry_device(hass, config_entry, device) is False
    response = await _ws_remove(hass, hass_ws_client, device.id)
    assert response["success"] is False
    assert response["error"]["message"] == REJECTED
    assert dev_reg.async_get(device.id) is not None
    assert hass.states.get("cover.kitchen") is not None


async def test_the_hub_device_cannot_be_deleted(hass, hass_ws_client, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id)

    assert await async_remove_config_entry_device(hass, config_entry, hub_device) is False
    response = await _ws_remove(hass, hass_ws_client, hub_device.id)
    assert response["success"] is False
    assert response["error"]["message"] == REJECTED
    assert dev_reg.async_get(hub_device.id) is not None


async def test_a_shade_the_hub_no_longer_reports_can_be_deleted(hass, hass_ws_client, config_entry):
    hub = await _setup(hass, config_entry)
    stale = _stale_device(hass, config_entry, "V81", "2nd floor stairs-top")
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get(stale.id) is not None, "the hub's update leaves a stale device alone"

    assert await async_remove_config_entry_device(hass, config_entry, stale) is True
    response = await _ws_remove(hass, hass_ws_client, stale.id)
    assert response["success"] is True, response
    assert dev_reg.async_get(stale.id) is None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "V81"), config_entry.entry_id) is None

    # The live shade and the hub are untouched, and the next update does not bring it back.
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "ABC"), config_entry.entry_id) is not None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id) is not None
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}})
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "V81"), config_entry.entry_id) is None
    assert hass.states.get("cover.kitchen") is not None


async def test_refused_while_the_hub_is_not_loaded_or_has_not_reported(hass, config_entry):
    """No live list to check against: refuse, never raise."""
    hub = await _setup(hass, config_entry)
    stale = _stale_device(hass, config_entry, "V81", "2nd floor stairs-top")

    # Set up, but the hub has not sent its first shadow (no id, no shades yet).
    assert hub.api.id is None
    assert await async_remove_config_entry_device(hass, config_entry, stale) is False

    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    assert await async_remove_config_entry_device(hass, config_entry, stale) is True, "sanity"

    # Unloaded: the hub object is gone from hass.data.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert await async_remove_config_entry_device(hass, config_entry, stale) is False

    # Loaded but the library object is missing (mid-reset).
    hass.data[DOMAIN][config_entry.entry_id] = hub
    assert hub.api is None
    assert await async_remove_config_entry_device(hass, config_entry, stale) is False
    hass.data[DOMAIN].pop(config_entry.entry_id)


async def test_a_device_without_one_of_our_identifiers_is_refused(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    odd = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("not_automate", "ABC")},
        name="Odd",
    )
    assert await async_remove_config_entry_device(hass, config_entry, odd) is False


async def test_the_check_is_per_hub(hass, hass_ws_client, config_entry):
    """Guest - main moved to the other hub: its old device on this hub's entry goes, the
    live one on the other hub's entry stays, and the same id is judged per entry."""
    hub = await _setup(hass, config_entry)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Other hub",
        data={"host": OTHER_HUB_HOST},
        unique_id=OTHER_HUB_ID,
    )
    other_entry.add_to_hass(hass)
    other_hub = await _setup(hass, other_entry)

    # This hub reports Kitchen only; the other hub now has the moved motor GM1 and GS1.
    await feed(hass, hub, {"ABC": {"rs": -82, "ol": True, "mp": 30}}, names={"ABC": "Kitchen"})
    await feed(
        hass,
        other_hub,
        {"GM1": {"rs": -70, "ol": True, "mp": 0}, "GS1": {"rs": -71, "ol": True, "mp": 0}},
        names={"GM1": "Guest - main", "GS1": "Guest - side"},
        hub_id=OTHER_HUB_ID,
        name="Guest hub",
    )
    stale_main = _stale_device(hass, config_entry, "GM1", "Guest - main")
    stale_side = _stale_device(hass, config_entry, "GS1", "Guest - side")

    dev_reg = dr.async_get(hass)
    live_main = dev_reg.async_get_device_by_identifier((DOMAIN, "GM1"), other_entry.entry_id)
    assert live_main is not None and live_main.id != stale_main.id, "one device per entry"
    assert dev_reg.async_get_device_by_identifier((DOMAIN, OTHER_HUB_ID), other_entry.entry_id)

    # The stale copies on this entry can go, although the same ids are live elsewhere.
    assert await async_remove_config_entry_device(hass, config_entry, stale_main) is True
    assert await async_remove_config_entry_device(hass, config_entry, stale_side) is True
    # The live ones on the other entry cannot, and this hub's live shade cannot either.
    assert await async_remove_config_entry_device(hass, other_entry, live_main) is False
    kitchen = dev_reg.async_get_device_by_identifier((DOMAIN, "ABC"), config_entry.entry_id)
    assert await async_remove_config_entry_device(hass, config_entry, kitchen) is False

    response = await _ws_remove(hass, hass_ws_client, stale_main.id)
    assert response["success"] is True, response
    assert dev_reg.async_get(stale_main.id) is None
    assert dev_reg.async_get(live_main.id) is not None
    assert er.async_get(hass).async_get("cover.guest_main").device_id == live_main.id
    assert hass.states.get("cover.guest_main") is not None
