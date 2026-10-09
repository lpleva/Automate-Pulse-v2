"""The Automate Pulse Hub v2 integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .hub import PulseHub

PLATFORMS = ["cover", "sensor"]

_LOGGER = logging.getLogger(__name__)


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
