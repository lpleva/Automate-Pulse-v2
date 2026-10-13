"""Code to handle a Pulse Hub."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    AUTOMATE_ENTITY_REMOVE,
    AUTOMATE_HUB_UPDATE,
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SIGNAL_POLL_INTERVAL,
    DOMAIN,
)
from .helpers import update_devices
from .vendor import aiopulse2

_LOGGER = logging.getLogger(__name__)


class PulseHub:
    """Manages a single Pulse Hub."""

    def __init__(self, hass, config_entry):
        """Initialize the system."""
        self.config_entry = config_entry
        self.hass = hass
        self.api: aiopulse2.Hub | None = None
        self.tasks: list[asyncio.Task] = []
        self.current_rollers = {}
        self.cleanup_callbacks = []
        self._entered_into_device_registry = False
        self.hub_device_id: str | None = None
        # The options this hub was built with; the update listener reloads the
        # entry only when they change.
        self.options_at_setup: dict = dict(config_entry.options)

    @property
    def title(self):
        """Return the title of the hub shown in the integrations list."""
        return f"{self.api.name} ({self.api.host})"

    @property
    def host(self):
        """Return the host of this hub."""
        return self.config_entry.data["host"]

    @property
    def poll_interval(self) -> float:
        """Seconds between shadow polls, from the entry options."""
        return self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

    @property
    def signal_poll_interval(self) -> float:
        """Seconds between live serial signal queries (0 = off), from the options."""
        return self.config_entry.options.get(
            CONF_SIGNAL_POLL_INTERVAL, DEFAULT_SIGNAL_POLL_INTERVAL
        )

    async def async_setup(self):
        """Set up a hub based on host parameter."""
        host = self.host

        hub = aiopulse2.Hub(
            host,
            propagate_callbacks=True,
            poll_interval=self.poll_interval,
            signal_poll_interval=self.signal_poll_interval,
        )

        self.api = hub

        hub.callback_subscribe(self.async_notify_update)
        self.tasks.append(asyncio.create_task(hub.run()))

        _LOGGER.debug(
            "Hub setup complete (shadow poll every %ss, live signal query every %ss)",
            hub.heartbeatinterval,
            hub.signal_poll_interval or "never",
        )
        return True

    async def async_reset(self):
        """Reset this hub to default state."""
        for cleanup_callback in self.cleanup_callbacks:
            cleanup_callback()
        self.cleanup_callbacks.clear()

        # If not setup
        if self.api is None:
            return False

        self.api.callback_unsubscribe(self.async_notify_update)
        await self.api.stop()
        self.api = None

        # The run task may be asleep in its reconnect back-off; do not wait it out.
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        return True

    def roller_for_unique_id(self, unique_id: str) -> aiopulse2.Roller | None:
        """The roller behind one of this hub's entity unique ids, or None.

        Every entity of a shade is keyed on the roller id: the cover is the id
        itself, the sensors and the button add a suffix (`<id>_signal`,
        `<id>_refresh_signal`, ...).
        """
        if self.api is None:
            return None
        for roller_id, roller in self.api.rollers.items():
            if unique_id == roller_id or unique_id.startswith(f"{roller_id}_"):
                return roller
        return None

    async def async_refresh_signal(self, roller: aiopulse2.Roller) -> bool:
        """Ask one shade for a live signal reading now; True when it answered.

        The same serial query the live sweep sends (`!<id>r?;` over port 1487);
        the reply updates the shade's signal and position through the usual
        path. Raises HomeAssistantError, with a message fit for a toast, when
        the query cannot be sent: the hub is disconnected, the shade is offline,
        or the hub's serial port cannot be reached. A shade that simply does not
        answer is not an error here: it returns False, and the caller decides.
        """
        name = roller.name or f"Shade {roller.id}"
        if self.api is None or not self.api.connected:
            raise HomeAssistantError(
                f"Cannot refresh the signal of {name}: the hub at {self.host} is not connected"
            )
        if not roller.online:
            raise HomeAssistantError(
                f"Cannot refresh the signal of {name}: the hub reports it offline"
            )
        try:
            return await self.api.query_signal(roller.id)
        except KeyError:
            raise HomeAssistantError(
                f"Cannot refresh the signal of {name}: it is no longer known to the hub"
            ) from None
        except (OSError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Cannot refresh the signal of {name}: the hub's serial port "
                f"{self.api.serial_port} could not be reached ({err})"
            ) from err

    def _hub_device_info(self) -> dict:
        """Device registry fields for the hub itself, from what it reported."""
        info = {
            "config_entry_id": self.config_entry.entry_id,
            "identifiers": {(DOMAIN, str(self.api.id))},
            "manufacturer": "Rollease Acmeda",
            "model": self.api.model or "Pulse Hub",
            "name": f"{self.api.name or 'Pulse Hub'} ({self.api.host})",
            "sw_version": self.api.firmware_ver,
        }
        if self.api.mac_address:
            info["connections"] = {
                (CONNECTION_NETWORK_MAC, format_mac(self.api.mac_address))
            }
        return info

    async def async_notify_update(self, hub=None):
        """Evaluate entities when hub reports that update has occurred."""
        if self.api is None:
            return
        _LOGGER.debug("Hub %s updated", self.title)

        # Check we have an ID, if not, wait for further updates
        if not self.api.id:
            _LOGGER.debug("Hub ID not yet available")
            return

        dev_registry = device_registry.async_get(self.hass)
        # Creates the hub device the first time, refreshes model / firmware / MAC
        # on later updates (async_get_or_create updates an existing device).
        device = dev_registry.async_get_or_create(**self._hub_device_info())
        self.hub_device_id = device.id
        if not self._entered_into_device_registry:
            _LOGGER.debug("Entered Hub %s into the device registry", self.title)
            self._entered_into_device_registry = True

        await update_devices(self.hass, self.config_entry, self.api.rollers)
        self.hass.config_entries.async_update_entry(self.config_entry, title=self.title)

        async_dispatcher_send(
            self.hass, AUTOMATE_HUB_UPDATE.format(self.config_entry.entry_id)
        )

        for unique_id in list(self.current_rollers):
            if unique_id not in self.api.rollers:
                _LOGGER.debug("Notifying remove of %s", unique_id)
                self.current_rollers.pop(unique_id)
                async_dispatcher_send(
                    self.hass, AUTOMATE_ENTITY_REMOVE.format(unique_id)
                )
