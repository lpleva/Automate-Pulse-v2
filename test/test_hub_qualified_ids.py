"""Shade ids are `<hub id>_<motor id>`: two hubs may both hold an "M4G".

The hub assigns three-character motor ids at pairing, so they are only unique
per hub. Found live 2026-11-04: the living room's right shade and the spare
bedroom's side shade, on two different hubs, are both "M4G". Every per-shade
unique id and device identifier therefore carries the hub id, and a version-1
install (bare motor ids) is migrated in place when the entry loads.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.automate import async_migrate_entry, async_remove_config_entry_device
from custom_components.automate.const import AUTOMATE_ENTITY_REMOVE, DOMAIN

from .conftest import HUB_HOST, HUB_ID, feed, settle, shade_id

pytestmark = pytest.mark.usefixtures("no_network")

OTHER_HUB_HOST = "192.0.2.11"
OTHER_HUB_ID = "2009999"

SHADE = {"rs": -82, "ol": True, "mp": 30, "vo": "12.1D23"}


async def _setup(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][config_entry.entry_id]


def _entry(hass, *, hub_id=HUB_ID, host=HUB_HOST, version=2, title="Test hub"):
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, data={"host": host}, unique_id=hub_id, version=version
    )
    entry.add_to_hass(hass)
    return entry


# --- the id format ------------------------------------------------------------------


async def test_every_shade_id_carries_the_hub_id(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"M4G": SHADE}, names={"M4G": "Living room right"})

    ent_reg = er.async_get(hass)
    expected = {
        "cover.living_room_right": shade_id("M4G"),
        "sensor.living_room_right_signal": shade_id("M4G", suffix="_signal"),
        "sensor.living_room_right_battery": shade_id("M4G", suffix="_battery"),
        "button.living_room_right_refresh": shade_id("M4G", suffix="_refresh_signal"),
    }
    for entity_id, unique_id in expected.items():
        entry = ent_reg.async_get(entity_id)
        assert entry is not None, (entity_id, hass.states.async_entity_ids())
        assert entry.unique_id == unique_id

    dev_reg = dr.async_get(hass)
    shade_device = dev_reg.async_get(ent_reg.async_get("cover.living_room_right").device_id)
    assert shade_device.identifiers == {(DOMAIN, shade_id("M4G"))}
    hub_device = dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id)
    assert hub_device is not None, "the hub's own device keeps the bare hub id"
    assert shade_device.via_device_id == hub_device.id
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "M4G"), config_entry.entry_id) is None

    assert hub.scope == HUB_ID
    assert hub.shade_identifier("M4G") == shade_id("M4G")


async def test_the_hub_resolves_only_its_own_ids(hass, config_entry):
    hub = await _setup(hass, config_entry)
    await feed(hass, hub, {"M4G": SHADE, "M4": SHADE}, names={"M4G": "Right", "M4": "Left"})
    roller = hub.api.rollers["M4G"]

    for unique_id in (shade_id("M4G"), shade_id("M4G", suffix="_signal"), shade_id("M4G", suffix="_refresh_signal")):
        assert hub.roller_for_unique_id(unique_id) is roller, unique_id
    # "M4" is a prefix of "M4G": a suffix never makes one motor look like another
    assert hub.roller_for_unique_id(shade_id("M4", suffix="_signal")) is hub.api.rollers["M4"]
    assert hub.roller_for_unique_id(shade_id("M4G")) is not hub.api.rollers["M4"]
    for unique_id in ("M4G", "M4G_signal", shade_id("M4G", hub_id=OTHER_HUB_ID), HUB_ID, shade_id("ZZZ")):
        assert hub.roller_for_unique_id(unique_id) is None, unique_id
    assert hub.roller_id_for_identifier(HUB_ID) is None, "the hub's own device"


async def test_an_entry_without_a_hub_id_is_scoped_by_its_entry_id(hass):
    entry = _entry(hass, hub_id=None)
    hub = await _setup(hass, entry)
    assert hub.scope == entry.entry_id
    await feed(hass, hub, {"M4G": SHADE}, names={"M4G": "Right"})
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("cover.right").unique_id == f"{entry.entry_id}_M4G"
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier((DOMAIN, f"{entry.entry_id}_M4G"), entry.entry_id)
    assert dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), entry.entry_id), "hub device unchanged"


# --- two hubs, one motor id ----------------------------------------------------------


async def _two_hubs_both_with_m4g(hass, config_entry):
    hub_a = await _setup(hass, config_entry)
    other_entry = _entry(hass, hub_id=OTHER_HUB_ID, host=OTHER_HUB_HOST, title="Other hub")
    hub_b = await _setup(hass, other_entry)
    await feed(hass, hub_a, {"M4G": SHADE}, names={"M4G": "Living room right"})
    await feed(
        hass,
        hub_b,
        {"M4G": dict(SHADE, mp=100)},
        names={"M4G": "Spare bedroom side"},
        hub_id=OTHER_HUB_ID,
        name="Spare bedroom hub",
    )
    return hub_a, hub_b, other_entry


async def test_two_hubs_with_the_same_motor_id_coexist(hass, config_entry):
    """The live case: living room right (hub A) and spare bedroom side (hub B) are both M4G."""
    hub_a, hub_b, other_entry = await _two_hubs_both_with_m4g(hass, config_entry)

    right = hass.states.get("cover.living_room_right")
    side = hass.states.get("cover.spare_bedroom_side")
    assert right is not None and side is not None, hass.states.async_entity_ids()
    assert right.attributes["current_position"] == 70
    assert side.attributes["current_position"] == 0, "each shows its own hub's position"
    for suffix in ("_signal", "_battery"):
        assert hass.states.get(f"sensor.living_room_right{suffix}") is not None
        assert hass.states.get(f"sensor.spare_bedroom_side{suffix}") is not None
    assert hass.states.get("button.spare_bedroom_side_refresh") is not None

    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("cover.living_room_right").unique_id == shade_id("M4G")
    assert ent_reg.async_get("cover.spare_bedroom_side").unique_id == shade_id("M4G", hub_id=OTHER_HUB_ID)

    dev_reg = dr.async_get(hass)
    device_a = dev_reg.async_get(ent_reg.async_get("cover.living_room_right").device_id)
    device_b = dev_reg.async_get(ent_reg.async_get("cover.spare_bedroom_side").device_id)
    assert device_a.id != device_b.id, "two devices, not one merged"
    assert device_a.name == "Living room right" and device_b.name == "Spare bedroom side"
    assert device_a.via_device_id == dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id).id
    assert device_b.via_device_id == dev_reg.async_get_device_by_identifier((DOMAIN, OTHER_HUB_ID), other_entry.entry_id).id
    # A device belongs to one config entry (`config_entry_id`); the old
    # `config_entries` set is deprecated in HA and raises when a test reads it.
    assert device_a.config_entry_id == config_entry.entry_id
    assert device_b.config_entry_id == other_entry.entry_id

    # An update on one hub's M4G leaves the other's alone.
    await feed(hass, hub_b, {"M4G": {"mp": 50}}, hub_id=OTHER_HUB_ID, name="Spare bedroom hub")
    assert hass.states.get("cover.spare_bedroom_side").attributes["current_position"] == 50
    assert hass.states.get("cover.living_room_right").attributes["current_position"] == 70

    # Each hub resolves its own M4G, and never the other's.
    assert hub_a.roller_for_unique_id(shade_id("M4G")) is hub_a.api.rollers["M4G"]
    assert hub_b.roller_for_unique_id(shade_id("M4G", hub_id=OTHER_HUB_ID)) is hub_b.api.rollers["M4G"]
    assert hub_a.roller_for_unique_id(shade_id("M4G", hub_id=OTHER_HUB_ID)) is None
    assert hub_b.roller_for_unique_id(shade_id("M4G")) is None


async def test_the_action_queries_each_hubs_own_m4g(hass, config_entry):
    hub_a, hub_b, _ = await _two_hubs_both_with_m4g(hass, config_entry)
    hub_a.async_refresh_signal = AsyncMock(return_value=True)
    hub_b.async_refresh_signal = AsyncMock(return_value=True)

    await hass.services.async_call(
        DOMAIN,
        "refresh_signal",
        {"entity_id": ["cover.spare_bedroom_side", "button.living_room_right_refresh"]},
        blocking=True,
    )
    hub_a.async_refresh_signal.assert_awaited_once_with(hub_a.api.rollers["M4G"])
    hub_b.async_refresh_signal.assert_awaited_once_with(hub_b.api.rollers["M4G"])


async def test_removing_one_hubs_m4g_leaves_the_other_hubs_m4g(hass, config_entry):
    """The remove signal is scoped to the hub; a bare motor id would hit both."""
    hub_a, hub_b, other_entry = await _two_hubs_both_with_m4g(hass, config_entry)

    async_dispatcher_send(hass, AUTOMATE_ENTITY_REMOVE.format(hub_a.shade_identifier("M4G")))
    await settle(hass)

    assert hass.states.get("cover.living_room_right") is None
    assert hass.states.get("sensor.living_room_right_signal") is None
    assert hass.states.get("cover.spare_bedroom_side") is not None, "the other hub's M4G stays"
    assert hass.states.get("sensor.spare_bedroom_side_signal") is not None
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("cover.living_room_right") is None
    assert ent_reg.async_get("cover.spare_bedroom_side") is not None
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier((DOMAIN, shade_id("M4G")), config_entry.entry_id) is None
    assert dev_reg.async_get_device_by_identifier(
        (DOMAIN, shade_id("M4G", hub_id=OTHER_HUB_ID)), other_entry.entry_id
    ) is not None


async def test_removing_a_shade_uses_the_current_device_registry_call(hass, config_entry):
    """HA deprecated `async_update_device(remove_config_entry_id=...)`; a shade that is
    gone is removed with `async_remove_device`, and the result is the same."""
    hub_a, _hub_b, other_entry = await _two_hubs_both_with_m4g(hass, config_entry)
    dev_reg = dr.async_get(hass)
    device_a = dev_reg.async_get_device_by_identifier((DOMAIN, shade_id("M4G")), config_entry.entry_id)
    deprecated = {"add_config_entry_id", "add_config_subentry_id", "remove_config_entry_id", "remove_config_subentry_id"}

    with (
        patch.object(dev_reg, "async_update_device", wraps=dev_reg.async_update_device) as update,
        patch.object(dev_reg, "async_remove_device", wraps=dev_reg.async_remove_device) as remove,
    ):
        async_dispatcher_send(hass, AUTOMATE_ENTITY_REMOVE.format(hub_a.shade_identifier("M4G")))
        await settle(hass)

    for call in update.call_args_list:
        assert not deprecated & set(call.kwargs), call
    remove.assert_called_once_with(device_a.id)
    assert dev_reg.async_get(device_a.id) is None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), config_entry.entry_id), "hub A's own device stays"
    assert dev_reg.async_get_device_by_identifier(
        (DOMAIN, shade_id("M4G", hub_id=OTHER_HUB_ID)), other_entry.entry_id
    ), "the other hub's M4G stays"
    ent_reg = er.async_get(hass)
    assert not [e.entity_id for e in ent_reg.entities.values() if e.device_id == device_a.id]


async def test_a_stale_device_is_judged_against_its_own_hub(hass, config_entry):
    """Hub A no longer reports M4G, hub B does: A's device can go, B's cannot."""
    hub_a, hub_b, other_entry = await _two_hubs_both_with_m4g(hass, config_entry)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device_a = dev_reg.async_get(ent_reg.async_get("cover.living_room_right").device_id)
    device_b = dev_reg.async_get(ent_reg.async_get("cover.spare_bedroom_side").device_id)

    # Live on both: neither can be deleted.
    assert await async_remove_config_entry_device(hass, config_entry, device_a) is False
    assert await async_remove_config_entry_device(hass, other_entry, device_b) is False

    # Hub A forgets M4G and its entities are gone (as after an unpair and a restart).
    hub_a.api.rollers.pop("M4G")
    for entity in er.async_entries_for_device(ent_reg, device_a.id, include_disabled_entities=True):
        ent_reg.async_remove(entity.entity_id)
    assert await async_remove_config_entry_device(hass, config_entry, device_a) is True
    assert await async_remove_config_entry_device(hass, other_entry, device_b) is False


# --- the migration, version 1 -> 2 ----------------------------------------------------


def _version_1_install(hass, entry, *, hub_id=HUB_ID, shades=("ABC",), stale=()):
    """The registries as a 1.5.x install left them: bare motor ids everywhere.

    Returns the hub device and `{motor id: (device, {entity_id: unique_id})}`.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    hub_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub_id)},
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")},
        manufacturer="Rollease Acmeda",
        model="Pulse Pro Hub",
        name=f"Living room hub ({HUB_HOST})",
    )
    installed = {}
    for roller_id in (*shades, *stale):
        device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, roller_id)},
            manufacturer="Rollease Acmeda",
            model="DC motor",
            name=f"Shade {roller_id}",
            via_device_id=hub_device.id,
        )
        entities = {}
        if roller_id in shades:
            slug = f"shade_{roller_id.lower()}"
            for domain, suffix, object_id in (
                ("cover", "", slug),
                ("sensor", "_signal", f"{slug}_signal"),
                ("sensor", "_battery", f"{slug}_battery"),
                ("button", "_refresh_signal", f"{slug}_refresh_signal"),
            ):
                reg_entry = ent_reg.async_get_or_create(
                    domain,
                    DOMAIN,
                    f"{roller_id}{suffix}",
                    config_entry=entry,
                    device_id=device.id,
                    suggested_object_id=object_id,
                )
                entities[reg_entry.entity_id] = reg_entry.unique_id
        installed[roller_id] = (device, entities)
    return hub_device, installed


async def test_migration_rewrites_the_ids_and_keeps_everything_visible(hass, caplog):
    entry = _entry(hass, version=1)
    hub_device, installed = _version_1_install(hass, entry, shades=("ABC", "M4G"), stale=("V81",))
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    kitchen_device, kitchen_entities = installed["ABC"]
    assert set(kitchen_entities) == {
        "cover.shade_abc",
        "sensor.shade_abc_signal",
        "sensor.shade_abc_battery",
        "button.shade_abc_refresh_signal",
    }
    # What a person customised: an area and a name on the device, a name and an
    # icon on the cover, a disabled battery sensor.
    area = ar.async_get(hass).async_create("Kitchen")
    dev_reg.async_update_device(kitchen_device.id, area_id=area.id, name_by_user="Kitchen shade")
    ent_reg.async_update_entity("cover.shade_abc", name="Kitchen blind", icon="mdi:blinds")
    ent_reg.async_update_entity("sensor.shade_abc_battery", disabled_by=er.RegistryEntryDisabler.USER)

    with caplog.at_level(logging.INFO):
        hub = await _setup(hass, entry)
    assert entry.version == 2
    assert "Migrated entry Test hub to version 2" in caplog.text

    # Entity ids unchanged, unique ids prefixed, same device, customisations kept.
    for roller_id in ("ABC", "M4G"):
        device, entities = installed[roller_id]
        for entity_id, old_unique_id in entities.items():
            reg_entry = ent_reg.async_get(entity_id)
            assert reg_entry is not None, entity_id
            assert reg_entry.unique_id == shade_id(roller_id) + old_unique_id[len(roller_id):]
            assert reg_entry.device_id == device.id
            assert ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, old_unique_id) is None
        migrated = dev_reg.async_get(device.id)
        assert migrated.identifiers == {(DOMAIN, shade_id(roller_id))}
        assert migrated.via_device_id == hub_device.id
        assert dev_reg.async_get_device_by_identifier((DOMAIN, roller_id), entry.entry_id) is None
    cover = ent_reg.async_get("cover.shade_abc")
    assert cover.name == "Kitchen blind" and cover.icon == "mdi:blinds"
    assert ent_reg.async_get("sensor.shade_abc_battery").disabled_by is er.RegistryEntryDisabler.USER
    kitchen = dev_reg.async_get(kitchen_device.id)
    assert kitchen.area_id == area.id and kitchen.name_by_user == "Kitchen shade"

    # The hub's own device is untouched.
    hub_after = dev_reg.async_get(hub_device.id)
    assert hub_after.identifiers == {(DOMAIN, HUB_ID)}
    assert hub_after.connections == hub_device.connections

    # The stale device (no entities) is prefixed too, and still deletable.
    stale_device, _ = installed["V81"]
    assert dev_reg.async_get(stale_device.id).identifiers == {(DOMAIN, shade_id("V81"))}

    # The hub reports: the live entities bind to the migrated registry entries,
    # under the same entity ids, with no "_2" copies.
    await feed(hass, hub, {"ABC": SHADE, "M4G": SHADE}, names={"ABC": "Kitchen", "M4G": "Living room right"})
    assert hass.states.get("cover.shade_abc") is not None
    assert hass.states.get("cover.shade_abc").attributes["friendly_name"] == "Kitchen blind"
    assert hass.states.get("cover.shade_m4g") is not None
    assert hass.states.get("sensor.shade_abc_signal").state == "-82"
    assert hass.states.get("sensor.shade_abc_battery") is None, "still disabled by the user"
    assert hass.states.get("cover.kitchen") is None and hass.states.get("cover.shade_abc_2") is None
    assert ent_reg.async_get("cover.shade_abc").device_id == kitchen_device.id
    assert dev_reg.async_get(kitchen_device.id).name == "Kitchen", "the hub's name still updates the device"
    assert dev_reg.async_get(kitchen_device.id).area_id == area.id
    assert len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)) == 4, "hub, ABC, M4G, V81: nothing new"
    assert await async_remove_config_entry_device(hass, entry, dev_reg.async_get(stale_device.id)) is True
    assert await async_remove_config_entry_device(hass, entry, dev_reg.async_get(kitchen_device.id)) is False


async def test_migration_runs_once_and_is_idempotent(hass):
    entry = _entry(hass, version=1)
    hub_device, installed = _version_1_install(hass, entry)
    device, entities = installed["ABC"]
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert ent_reg.async_get("cover.shade_abc").unique_id == shade_id("ABC")

    # Run again at version 2: nothing to do. Then forced back to version 1, as a
    # restored older config_entries file with a newer registry would be: the
    # already-prefixed ids are not prefixed twice.
    assert await async_migrate_entry(hass, entry) is True
    hass.config_entries.async_update_entry(entry, version=1)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert ent_reg.async_get("cover.shade_abc").unique_id == shade_id("ABC")
    assert ent_reg.async_get("sensor.shade_abc_signal").unique_id == shade_id("ABC", suffix="_signal")
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, shade_id("ABC"))}
    assert dev_reg.async_get(hub_device.id).identifiers == {(DOMAIN, HUB_ID)}


async def test_an_already_migrated_entry_is_left_alone(hass):
    """A version-2 install: no migration, no registry writes, the ids as they are."""
    entry = _entry(hass, version=2)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, HUB_ID)}, name="Hub"
    )
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, shade_id("ABC"))},
        name="Kitchen",
        via_device_id=hub_device.id,
    )
    cover = ent_reg.async_get_or_create(
        "cover", DOMAIN, shade_id("ABC"), config_entry=entry, device_id=device.id, suggested_object_id="kitchen"
    )
    before = (cover.modified_at, device.modified_at)

    hub = await _setup(hass, entry)
    assert entry.version == 2
    assert ent_reg.async_get("cover.kitchen").unique_id == shade_id("ABC")
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, shade_id("ABC"))}
    assert (ent_reg.async_get("cover.kitchen").modified_at, dev_reg.async_get(device.id).modified_at) == before

    await feed(hass, hub, {"ABC": SHADE}, names={"ABC": "Kitchen"})
    assert hass.states.get("cover.kitchen") is not None
    assert ent_reg.async_get("cover.kitchen").device_id == device.id


async def test_migration_of_an_entry_without_a_hub_id(hass):
    """No unique id on the entry: the scope is the entry id, and the hub device is
    told from the shades by being the one that hangs off nothing."""
    entry = _entry(hass, hub_id=None, version=1)
    hub_device, installed = _version_1_install(hass, entry, stale=("V81",))
    device, _ = installed["ABC"]
    stale_device, _ = installed["V81"]
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    hub = await _setup(hass, entry)
    assert entry.version == 2
    scope = entry.entry_id
    assert hub.scope == scope
    assert ent_reg.async_get("cover.shade_abc").unique_id == f"{scope}_ABC"
    assert ent_reg.async_get("sensor.shade_abc_signal").unique_id == f"{scope}_ABC_signal"
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, f"{scope}_ABC")}
    assert dev_reg.async_get(stale_device.id).identifiers == {(DOMAIN, f"{scope}_V81")}
    assert dev_reg.async_get(hub_device.id).identifiers == {(DOMAIN, HUB_ID)}, "the hub keeps its id"

    await feed(hass, hub, {"ABC": SHADE}, names={"ABC": "Kitchen"})
    assert hass.states.get("cover.shade_abc") is not None
    assert hass.states.get("cover.kitchen") is None
    assert ent_reg.async_get("cover.shade_abc").device_id == device.id
    assert dev_reg.async_get_device_by_identifier((DOMAIN, HUB_ID), entry.entry_id).id == hub_device.id


async def test_migration_skips_an_id_that_is_already_taken(hass, caplog):
    """A registry half migrated, then an older release run on it: both the bare
    and the prefixed id exist. The bare one is left, with a warning, never a crash."""
    entry = _entry(hass, version=1)
    hub_device, installed = _version_1_install(hass, entry)
    old_device, _ = installed["ABC"]
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    new_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, shade_id("ABC"))},
        name="Shade ABC",
        via_device_id=hub_device.id,
    )
    ent_reg.async_get_or_create(
        "cover", DOMAIN, shade_id("ABC"), config_entry=entry, device_id=new_device.id, suggested_object_id="shade_abc_2"
    )

    with caplog.at_level(logging.WARNING):
        hub = await _setup(hass, entry)
    assert entry.version == 2
    assert "Not migrating cover.shade_abc: its new unique id" in caplog.text
    assert "already held by cover.shade_abc_2" in caplog.text
    assert f"Not migrating device {old_device.id}" in caplog.text
    assert ent_reg.async_get("cover.shade_abc").unique_id == "ABC", "left as it was"
    assert ent_reg.async_get("cover.shade_abc_2").unique_id == shade_id("ABC")
    # The sensors and the button had no rival, so they moved.
    assert ent_reg.async_get("sensor.shade_abc_signal").unique_id == shade_id("ABC", suffix="_signal")
    assert dev_reg.async_get(old_device.id).identifiers == {(DOMAIN, "ABC")}
    assert dev_reg.async_get(new_device.id).identifiers == {(DOMAIN, shade_id("ABC"))}

    # The entry loads and the live shade binds to the prefixed entry. The bare
    # device is not the hub's and not live, but it still owns the orphaned
    # cover entry, so Delete is refused until that entry is removed (the 1.5.0
    # rule); then it can go.
    await feed(hass, hub, {"ABC": SHADE}, names={"ABC": "Kitchen"})
    assert hass.states.get("cover.shade_abc_2") is not None
    assert hass.states.get("cover.shade_abc") is None
    assert await async_remove_config_entry_device(hass, entry, dev_reg.async_get(old_device.id)) is False
    ent_reg.async_remove("cover.shade_abc")
    assert await async_remove_config_entry_device(hass, entry, dev_reg.async_get(old_device.id)) is True


async def test_an_entry_from_a_newer_release_is_refused(hass):
    entry = _entry(hass, version=3)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert await async_migrate_entry(hass, entry) is False
