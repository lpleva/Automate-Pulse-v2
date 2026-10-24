"""The config entry's title: the integration names a fresh entry, never a renamed one."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.automate.const import (
    CONF_AUTO_TITLE,
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    DOMAIN,
)

from .conftest import HUB_HOST, HUB_ID, feed

pytestmark = pytest.mark.usefixtures("no_network")

OTHER_HOST = "192.0.2.11"
SHADES = {"ABC": {"rs": -82, "ol": True, "mp": 30}}
# Ends in "(<host>)" on purpose, like Lukas's own titles do: the host suffix alone
# must not make a title look auto-generated.
RENAMED = f"Shades hub - first floor living area ({HUB_HOST})"


def _entry(hass, title, host=HUB_HOST, **data):
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, data={"host": host, **data}, unique_id=HUB_ID
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][config_entry.entry_id]


async def _restart(hass, config_entry):
    """Unload and set up again, as a Home Assistant restart would."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    return await _setup(hass, config_entry)


async def _update(hass, hub, **extra):
    """One hub update that reaches the title step.

    The library holds its callbacks until every shade has a name, so the shade is
    named on every push (a fresh hub object after a reload knows no names), and
    the hub device id proves the update ran rather than being held.
    """
    await feed(hass, hub, SHADES, names={"ABC": "Kitchen"}, **extra)
    assert hub.hub_device_id is not None, "the hub update was held, nothing was checked"


@pytest.mark.parametrize(
    "title",
    [
        "Living room hub",  # the config flow, hub named
        f"Pulse Hub ({HUB_HOST})",  # the config flow, hub unnamed
        f"Living room hub ({HUB_HOST})",  # a release before the record, hub named
        f"None ({HUB_HOST})",  # a release before the record, hub unnamed at the time
    ],
)
async def test_a_fresh_entry_gets_the_auto_title(hass, title):
    entry = _entry(hass, title)
    hub = await _setup(hass, entry)
    await _update(hass, hub)

    assert entry.title == f"Living room hub ({HUB_HOST})"
    assert entry.data[CONF_AUTO_TITLE] == entry.title, "the title written is recorded"
    assert entry.data["host"] == HUB_HOST
    hub_device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, HUB_ID), entry.entry_id)
    assert hub_device.name == entry.title, "entry title and hub device name agree"


async def test_an_unnamed_hub_is_titled_by_host_until_its_name_arrives(hass):
    entry = _entry(hass, f"Pulse Hub ({HUB_HOST})")
    hub = await _setup(hass, entry)
    await _update(hass, hub, name=None)
    assert entry.title == f"Pulse Hub ({HUB_HOST})"

    await _update(hass, hub)
    assert entry.title == f"Living room hub ({HUB_HOST})"


async def test_a_renamed_entry_keeps_its_title(hass):
    entry = _entry(hass, "Living room hub")
    hub = await _setup(hass, entry)
    await _update(hass, hub)
    assert entry.title == f"Living room hub ({HUB_HOST})"

    # Settings, Devices & services, the entry, Rename
    hass.config_entries.async_update_entry(entry, title=RENAMED)
    await hass.async_block_till_done()

    # the next hub update
    await _update(hass, hub)
    assert entry.title == RENAMED

    # a reload from the options flow (what reverted the title live, 2026-10-24)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_SIGNAL_POLL_INTERVAL: 0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    hub = hass.data[DOMAIN][entry.entry_id]
    await _update(hass, hub)
    assert entry.title == RENAMED

    # a restart, with the hub renamed and moved in the meantime
    hub = await _restart(hass, entry)
    await _update(hass, hub, name="My House")
    assert entry.title == RENAMED
    assert entry.data[CONF_AUTO_TITLE] == f"Living room hub ({HUB_HOST})", (
        "the record stays what the integration last wrote"
    )


async def test_a_title_the_integration_never_wrote_is_left_alone(hass, config_entry):
    """No record and not one of our forms: a person set it, even if we cannot say when."""
    hub = await _setup(hass, config_entry)
    await _update(hass, hub)
    assert config_entry.title == "Test hub"
    assert CONF_AUTO_TITLE not in config_entry.data


async def test_an_auto_title_follows_the_hubs_name(hass):
    entry = _entry(hass, "Living room hub")
    hub = await _setup(hass, entry)
    await _update(hass, hub)
    assert entry.title == f"Living room hub ({HUB_HOST})"

    await _update(hass, hub, name="Kitchen hub")
    assert entry.title == f"Kitchen hub ({HUB_HOST})"
    assert entry.data[CONF_AUTO_TITLE] == entry.title
    assert hass.data[DOMAIN][entry.entry_id] is hub, "a title change does not reload the entry"


async def test_an_auto_title_follows_the_hubs_host(hass):
    entry = _entry(hass, "Living room hub")
    hub = await _setup(hass, entry)
    await _update(hass, hub)
    assert entry.title == f"Living room hub ({HUB_HOST})"

    # The hub moved: the config flow's already_configured path updates the host
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "host": OTHER_HOST})
    await hass.async_block_till_done()
    hub = await _setup(hass, entry)
    await _update(hass, hub)

    assert entry.title == f"Living room hub ({OTHER_HOST})"
    assert entry.data[CONF_AUTO_TITLE] == entry.title


async def test_the_hub_devices_user_name_survives_updates(hass):
    entry = _entry(hass, "Living room hub")
    hub = await _setup(hass, entry)
    await _update(hass, hub)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), entry.entry_id)
    dev_reg.async_update_device(device.id, name_by_user="First floor shades")

    await _update(hass, hub, name="Kitchen hub")
    hub = await _restart(hass, entry)
    await _update(hass, hub, name="Kitchen hub")

    device = dev_reg.async_get(device.id)
    assert device.name_by_user == "First floor shades"
    assert device.name == f"Kitchen hub ({HUB_HOST})", "the integration's own name still follows the hub"
