"""The Automate Pulse Hub v2 integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .helpers import entry_scope, shade_identifier
from .hub import PulseHub
from .services import async_setup_services

PLATFORMS = ["button", "cover", "sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions; they exist whether or not a hub is set up."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry's registries up to the current id scheme.

    Version 1 -> 2 (fork 1.6.0): every per-shade unique id and device
    identifier was the bare motor id ("M4G"), which the hub assigns at pairing
    and which is therefore only unique per hub. Two hubs holding the same motor
    id made Home Assistant drop the second hub's entities as duplicates. The
    ids are now `<scope>_<motor id>` (helpers.entry_scope: the hub id, or the
    entry id for an entry without one), and this rewrites the registries in
    place so nothing visible changes: entity ids, names, areas, labels,
    history and dashboards all stay, because only the registries' unique_id
    and identifiers fields move.

    The hub's own device keeps its identifier (the bare hub id): it is the one
    whose identifier is the entry's unique id, or, for an entry without one,
    the only device that does not hang off another (`via_device_id`), since
    every shade device is created under the hub's.

    Safe to run again: an id that already carries the scope is left alone, and
    a target id that is already taken (a registry half migrated, then an older
    release run on it) is logged and skipped rather than failing the entry.
    """
    if entry.version > 2:
        return False
    if entry.version == 1:
        _migrate_ids_to_hub_scope(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info("Migrated entry %s to version 2 (hub-qualified shade ids)", entry.title)
    return True


def _migrate_ids_to_hub_scope(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Prefix this entry's shade entity unique ids and device identifiers with its scope."""
    scope = entry_scope(entry)
    prefix = f"{scope}_"
    ent_reg = er.async_get(hass)

    def migrate_entity(reg_entry: er.RegistryEntry) -> dict | None:
        if reg_entry.unique_id.startswith(prefix):
            return None
        new_unique_id = f"{prefix}{reg_entry.unique_id}"
        taken = ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id)
        if taken is not None:
            _LOGGER.warning(
                "Not migrating %s: its new unique id %s is already held by %s; "
                "the old entry is left as it is and can be deleted",
                reg_entry.entity_id,
                new_unique_id,
                taken,
            )
            return None
        return {"new_unique_id": new_unique_id}

    er.async_migrate_entries(hass, entry.entry_id, migrate_entity)

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if _is_hub_device(device, entry):
            continue
        new_identifiers = set()
        for domain, ident in device.identifiers:
            if domain == DOMAIN and not ident.startswith(prefix):
                ident = shade_identifier(scope, ident)
            new_identifiers.add((domain, ident))
        if new_identifiers == device.identifiers:
            continue
        taken = next(
            (
                other
                for domain, ident in new_identifiers
                if domain == DOMAIN
                and (other := dev_reg.async_get_device_by_identifier((domain, ident), entry.entry_id))
                is not None
                and other.id != device.id
            ),
            None,
        )
        if taken is not None:
            _LOGGER.warning(
                "Not migrating device %s (%s): its new identifier is already held by "
                "device %s; the old device is left as it is and can be deleted",
                device.id,
                device.name_by_user or device.name,
                taken.id,
            )
            continue
        dev_reg.async_update_device(device.id, new_identifiers=new_identifiers)


def _is_hub_device(device: DeviceEntry, entry: ConfigEntry) -> bool:
    """Whether a device of this entry is the hub's own, which keeps its bare identifier."""
    if entry.unique_id is not None:
        return (DOMAIN, entry.unique_id) in device.identifiers
    return device.via_device_id is None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Automate Pulse Hub v2 from a config entry."""
    hub = PulseHub(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    # Platforms first, so their dispatcher listeners exist before the hub's first
    # update arrives (upstream f9015a6); then the hub's connection task.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not await hub.async_setup():
        return False

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options or its host changed.

    Options: a changed polling interval only takes effect on a fresh hub.
    Host: the config flow writes the new address when it finds an already
    configured hub there (`_abort_if_unique_id_configured(updates=...)`), and
    Home Assistant has said it will stop reloading the entry itself there in
    2026.12 for an integration that has an update listener, so this listener
    does it.

    HA fires this listener on ANY entry update, and the hub writes the entry
    itself when its auto-generated title changes (title and the `auto_title`
    record in data), so anything else is left alone. No hub means the entry is
    unloading (unload pops it before HA drops this listener): nothing to do,
    and a reload now would set the entry up again or fail on a removed one.
    """
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is None:
        return
    if (
        dict(entry.options) == hub.options_at_setup
        and entry.data["host"] == hub.host_at_setup
    ):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hub = hass.data[DOMAIN][entry.entry_id]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    await hub.async_reset()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> bool:
    """Allow Settings > Devices > Delete on a device this hub no longer reports.

    A shade unpaired in the Pulse app, or moved to another hub, leaves its device
    behind after a restart: the hub never lists it again, so nothing removes it.
    This lets the user delete it. Refused for the hub's own device and for any
    shade the hub still reports (HA would recreate that one on the next update,
    and the user would lose its customisations), refused for a device that still
    has entity registry entries under this entry (a live shade whose roller is
    missing from the list for a moment, as after a truncated first shadow on
    restart, still has its cover and sensors; a device left behind has none),
    and refused whenever this entry's hub is not loaded or has not reported yet,
    since then there is no live list to check against.

    Every device of this integration carries one `(DOMAIN, <id>)` identifier,
    the hub's `str(hub.id)` or the shade's `<hub id>_<motor id>`. Devices belong
    to one config entry each, so an id that is live on ANOTHER hub's entry does
    not keep this entry's device; only this entry's hub is consulted. A shade
    device still carrying a bare motor id (one the migration could not place)
    is never live, so it can go.
    """
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is None or hub.api is None or not hub.api.id:
        _LOGGER.debug(
            "Refusing to remove device %s: the hub of entry %s is not loaded "
            "or has not reported yet",
            device.id,
            entry.entry_id,
        )
        return False

    ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    if not ours:
        _LOGGER.debug(
            "Refusing to remove device %s: none of %s is one of this "
            "integration's identifiers",
            device.id,
            sorted(device.identifiers),
        )
        return False
    if str(hub.api.id) in ours:
        _LOGGER.debug("Refusing to remove device %s: it is the hub itself", device.id)
        return False
    live = ours & {hub.shade_identifier(roller_id) for roller_id in hub.api.rollers}
    if live:
        _LOGGER.debug(
            "Refusing to remove device %s: shade %s is still reported by the hub",
            device.id,
            sorted(live),
        )
        return False
    entities = [
        e
        for e in er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True
        )
        if e.config_entry_id == entry.entry_id
    ]
    if entities:
        _LOGGER.debug(
            "Refusing to remove device %s: it still has entities (%s), so the "
            "shade is live even though the hub's list is missing it right now",
            device.id,
            sorted(e.entity_id for e in entities),
        )
        return False
    _LOGGER.info(
        "Removing device %s (%s): the hub no longer reports shade %s",
        device.id,
        device.name_by_user or device.name,
        sorted(ours),
    )
    return True
