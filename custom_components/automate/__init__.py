"""The Automate Pulse Hub v2 integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .hub import PulseHub
from .services import async_setup_services

PLATFORMS = ["button", "cover", "sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions; they exist whether or not a hub is set up."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Automate Pulse Hub v2 from a config entry."""
    hub = PulseHub(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    # Platforms first, so their dispatcher listeners exist before the hub's first
    # update arrives (upstream f9015a6); then the hub's connection task.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not await hub.async_setup():
        return False

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so a changed polling interval takes effect.

    HA fires this listener on ANY entry update, and the hub renames the entry's
    title on every update, so reload only when the options actually changed.
    """
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is not None and dict(entry.options) == hub.options_at_setup:
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
    the hub's `str(hub.id)` or the shade's roller id. Devices belong to one
    config entry each, so an id that is live on ANOTHER hub's entry does not
    keep this entry's device; only this entry's hub is consulted.
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
    live = ours & set(hub.api.rollers)
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
