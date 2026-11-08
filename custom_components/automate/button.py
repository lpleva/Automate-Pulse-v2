"""A "Refresh" button per shade: signal, position and battery, on demand."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .base import AutomateBase
from .const import AUTOMATE_HUB_UPDATE, DOMAIN
from .helpers import async_add_automate_entities


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the per-shade buttons from a config entry."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    current = set()

    @callback
    def async_add_automate_buttons():
        async_add_automate_entities(
            hass, AutomateRefresh, config_entry, current, async_add_entities
        )

    hub.cleanup_callbacks.append(
        async_dispatcher_connect(
            hass,
            AUTOMATE_HUB_UPDATE.format(config_entry.entry_id),
            async_add_automate_buttons,
        )
    )


class AutomateRefresh(AutomateBase, ButtonEntity):
    """Press to refresh this shade's signal, position and battery right now.

    Two read queries through the hub's existing connections (PulseHub.async_refresh):
    the serial `!<id>r?;` query the live sweep sends, whose reply updates the
    Signal sensor and the cover position at once, and the websocket shadow
    `query` for the shade, after which the hub re-reads the motor and the next
    shadow poll brings a fresh battery voltage. It never moves the shade or
    touches its settings. Diagnostic, so Home Assistant keeps it off
    auto-generated dashboards.

    Until 1.6.1 this was "Refresh signal" and sent the serial query alone. The
    unique id suffix is kept, so an existing install keeps its entity id
    (`button.<shade>_refresh_signal`) and whatever refers to it; a shade first
    seen by this release gets `button.<shade>_refresh`.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"
    _attr_name = "Refresh"
    _unique_id_suffix = "_refresh_signal"

    async def async_press(self) -> None:
        """Refresh the shade; the hub raises a clear HomeAssistantError naming what failed."""
        hub = self.hass.data[DOMAIN][self.config_entry_id]
        await hub.async_refresh(self.roller)
