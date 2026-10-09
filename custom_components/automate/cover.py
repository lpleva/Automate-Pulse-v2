"""Support for Automate Roller Blinds."""

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .base import AutomateBase
from .const import AUTOMATE_HUB_UPDATE, DOMAIN
from .helpers import async_add_automate_entities
from .vendor import aiopulse2


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Automate Rollers from a config entry."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    current = set()

    @callback
    def async_add_automate_covers():
        async_add_automate_entities(
            hass, AutomateCover, config_entry, current, async_add_entities
        )

    hub.cleanup_callbacks.append(
        async_dispatcher_connect(
            hass,
            AUTOMATE_HUB_UPDATE.format(config_entry.entry_id),
            async_add_automate_covers,
        )
    )


class AutomateCover(AutomateBase, CoverEntity):
    """Representation of a Automate cover device."""

    _attr_device_class = CoverDeviceClass.SHADE

    @property
    def current_cover_position(self):
        """Return the current position of the roller blind.

        None is unknown, 0 is closed, 100 is fully open.
        """
        position = None
        if self.roller.closed_percent is not None:
            position = 100 - self.roller.closed_percent
        return position

    @property
    def supported_features(self):
        """Flag supported features."""
        supported_features = CoverEntityFeature(0)
        if self.current_cover_position is not None:
            supported_features |= (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
                | CoverEntityFeature.SET_POSITION
            )
        return supported_features

    @property
    def is_opening(self):
        """Is cover opening/moving up."""
        return self.roller.action == aiopulse2.MovingAction.up

    @property
    def is_closing(self):
        """Is cover closing/moving down."""
        return self.roller.action == aiopulse2.MovingAction.down

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self.roller.closed_percent == 100

    async def async_close_cover(self, **kwargs):
        """Close the roller."""
        await self.roller.move_down()

    async def async_open_cover(self, **kwargs):
        """Open the roller."""
        await self.roller.move_up()

    async def async_stop_cover(self, **kwargs):
        """Stop the roller."""
        await self.roller.move_stop()

    async def async_set_cover_position(self, **kwargs):
        """Move the roller shutter to a specific position."""
        await self.roller.move_to(100 - kwargs[ATTR_POSITION])
