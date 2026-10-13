"""A "Refresh signal" button per shade: one live signal query, on demand."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
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
            hass, AutomateRefreshSignal, config_entry, current, async_add_entities
        )

    hub.cleanup_callbacks.append(
        async_dispatcher_connect(
            hass,
            AUTOMATE_HUB_UPDATE.format(config_entry.entry_id),
            async_add_automate_buttons,
        )
    )


class AutomateRefreshSignal(AutomateBase, ButtonEntity):
    """Press to ask this shade for a live signal reading right now.

    Sends exactly what the live sweep sends for the shade on its turn (the
    serial `!<id>r?;` query); the reply updates the shade's Signal sensor
    through the usual path. It never moves the shade or touches its settings.
    Diagnostic, so Home Assistant keeps it off auto-generated dashboards.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi-refresh"
    _attr_name = "Refresh signal"

    @property
    def unique_id(self):
        """Return a unique identifier for this button."""
        return f"{self.roller.id}_refresh_signal"

    async def async_press(self) -> None:
        """Query the shade; a clear error when the hub, shade or port cannot be reached."""
        hub = self.hass.data[DOMAIN][self.config_entry_id]
        replied = await hub.async_refresh_signal(self.roller)
        if not replied:
            raise HomeAssistantError(
                f"{self.roller.name} did not answer the signal query "
                "(the hub could not reach it over radio)"
            )
