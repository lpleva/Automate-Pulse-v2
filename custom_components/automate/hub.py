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
    CONF_AUTO_TITLE,
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SIGNAL_POLL_INTERVAL,
    DOMAIN,
)
from .helpers import entry_scope, shade_identifier, update_devices
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
        # The options and host this hub was built with; the update listener
        # reloads the entry only when one of them changes. The host is set
        # again in async_setup, from the value the library hub is given.
        self.options_at_setup: dict = dict(config_entry.options)
        self.host_at_setup: str = config_entry.data["host"]

    @property
    def title(self):
        """The entry title this integration gives the hub: `<hub name> (<host>)`.

        The same text as the hub device's name, so the integrations list and
        the device page agree.
        """
        return f"{self.api.name or 'Pulse Hub'} ({self.api.host})"

    def _title_is_ours(self) -> bool:
        """True while the entry's title is one this integration wrote, not a person.

        The last title the hub wrote is recorded in the entry's data (`auto_title`),
        and the title is ours exactly while it still equals that record. An entry
        with no record yet (created by the config flow, or set up by a release
        before the record existed) is ours when its title is one the integration
        itself produces: the config flow's bare hub name or `Pulse Hub (<host>)`,
        or the `<hub name> (<host>)` form, including the `None (<host>)` an older
        release wrote for a hub whose name had not arrived. Anything else was
        typed by a person in Home Assistant and must never be overwritten.
        """
        current = self.config_entry.title
        recorded = self.config_entry.data.get(CONF_AUTO_TITLE)
        if recorded is not None:
            return current == recorded
        host = self.api.host
        return current in {
            self.title,
            f"None ({host})",
            self.api.name,
            f"Pulse Hub ({host})",
        }

    def _async_update_title(self) -> None:
        """Refresh the entry title from the hub's name and host, unless a person set it.

        Writes the entry only when the title is still ours and has changed
        (Home Assistant drops an update that changes nothing, so a steady title
        costs no storage write and fires no update listener).
        """
        if not self._title_is_ours():
            return
        title = self.title
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=title,
            data={**self.config_entry.data, CONF_AUTO_TITLE: title},
        )

    @property
    def host(self):
        """Return the host of this hub."""
        return self.config_entry.data["host"]

    @property
    def scope(self) -> str:
        """The prefix on every id of this hub's shades (the hub id); see helpers.entry_scope."""
        return entry_scope(self.config_entry)

    def shade_identifier(self, roller_id: str) -> str:
        """The device identifier (and cover unique id) of one of this hub's shades."""
        return shade_identifier(self.scope, roller_id)

    def roller_id_for_identifier(self, identifier: str) -> str | None:
        """The motor id behind one of this hub's shade identifiers or entity unique ids.

        `<scope>_<motor id>` and `<scope>_<motor id>_<suffix>` both give the motor
        id. None for an id that is not under this hub's scope: a hub's own device
        identifier, or a shade of another hub. The motor id is matched against
        what the hub reports, so a suffix can never be mistaken for part of it.
        """
        prefix = f"{self.scope}_"
        if self.api is None or not identifier.startswith(prefix):
            return None
        rest = identifier[len(prefix) :]
        for roller_id in self.api.rollers:
            if rest == roller_id or rest.startswith(f"{roller_id}_"):
                return roller_id
        return None

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
        # Read once, here: the platforms were set up first and yielded to the
        # loop, so a host written in that window must be the one both the
        # library hub and the update listener's comparison use.
        host = self.host_at_setup = self.host

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

        Every entity of a shade is keyed on `<hub id>_<motor id>`: the cover is
        that itself, the sensors and the button add a suffix (`..._signal`,
        `..._refresh_signal`, ...).
        """
        roller_id = self.roller_id_for_identifier(unique_id)
        if roller_id is None:
            return None
        return self.api.rollers[roller_id]

    @staticmethod
    def _shade_name(roller: aiopulse2.Roller) -> str:
        return roller.name or f"Shade {roller.id}"

    def _check_shade_reachable(self, roller: aiopulse2.Roller, what: str) -> None:
        """Raise a HomeAssistantError fit for a toast when no query can reach the shade.

        Both query paths need the hub connected and the shade online (the hub
        relays either one over radio), and a shade the hub has dropped has no
        path at all. `what` names the refresh in the message ("the signal of
        Kitchen", "Kitchen").
        """
        if self.api is None or not self.api.connected:
            raise HomeAssistantError(
                f"Cannot refresh {what}: the hub at {self.host} is not connected"
            )
        if roller.id not in self.api.rollers:
            raise HomeAssistantError(f"Cannot refresh {what}: it is no longer known to the hub")
        if not roller.online:
            raise HomeAssistantError(f"Cannot refresh {what}: the hub reports it offline")

    async def async_refresh_signal(self, roller: aiopulse2.Roller) -> bool:
        """Ask one shade for a live signal reading now; True when it answered.

        The same serial query the live sweep sends (`!<id>r?;` over port 1487);
        the reply updates the shade's signal and position through the usual
        path. Raises HomeAssistantError, with a message fit for a toast, when
        the query cannot be sent: the hub is disconnected, the shade is offline,
        or the hub's serial port cannot be reached. A shade that simply does not
        answer is not an error here: it returns False, and the caller decides.
        The battery is not touched; that is async_refresh.
        """
        what = f"the signal of {self._shade_name(roller)}"
        self._check_shade_reachable(roller, what)
        try:
            return await self.api.query_signal(roller.id)
        except KeyError:
            raise HomeAssistantError(
                f"Cannot refresh {what}: it is no longer known to the hub"
            ) from None
        except (OSError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Cannot refresh {what}: the hub's serial port "
                f"{self.api.serial_port} could not be reached ({err})"
            ) from err

    async def async_refresh(self, roller: aiopulse2.Roller) -> None:
        """Refresh one shade's signal, position AND battery now.

        Two read queries, both over the library's existing connections: the
        serial `!<id>r?;` query (signal and position land from the reply at
        once), then the websocket shadow `query: true` for the shade (the hub
        re-reads the motor, and the next shadow poll brings a fresh voltage,
        which the battery sensor follows). Serial first, so the reply or the
        miss is in hand before the hub is asked to talk to the same motor: the
        two radio exchanges do not overlap. Both parts are always attempted
        once the shade is reachable at all; a failure of one never stops the
        other. Returns quietly when everything worked. Otherwise raises ONE
        HomeAssistantError, fit for a toast, naming the shade, each part that
        failed and why, and what did work, so neither part's result hides the
        other's. The battery query is spaced by the library (one per shade per
        poll interval); a press inside that window shares the pending request
        and is not a failure.
        """
        name = self._shade_name(roller)
        self._check_shade_reachable(roller, name)
        failed: list[str] = []
        worked: list[str] = []

        replied = False
        try:
            replied = await self.api.query_signal(roller.id)
        except KeyError:
            raise HomeAssistantError(
                f"Cannot refresh {name}: it is no longer known to the hub"
            ) from None
        except (OSError, asyncio.TimeoutError) as err:
            failed.append(
                "the signal query could not be sent (the hub's serial port "
                f"{self.api.serial_port} could not be reached: {err})"
            )
        else:
            if replied:
                worked.append("the signal query was answered")
            else:
                failed.append(
                    "it did not answer the signal query (the hub could not reach it over radio)"
                )

        try:
            sent = await self.api.query_details(roller.id)
        except KeyError:
            raise HomeAssistantError(
                f"Cannot refresh {name}: it is no longer known to the hub"
            ) from None
        except (aiopulse2.NotConnectedException, aiopulse2.NotRunningException):
            failed.append(
                "the battery query could not be sent (the hub's websocket is not connected)"
            )
        else:
            if sent:
                worked.append(
                    "the battery query was sent, so the next hub poll may bring a fresh reading"
                )
            else:
                worked.append(
                    "a battery query from the last "
                    f"{self.api.details_query_spacing():.0f} s is still pending, "
                    "so the next hub poll may bring a fresh reading"
                )

        if not failed:
            return
        message = f"Cannot refresh {name}: " + "; ".join(failed)
        if worked:
            message += ". Still, " + "; ".join(worked)
        raise HomeAssistantError(message)

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
        # Creates the hub device the first time, refreshes name / model / firmware
        # / MAC on later updates (async_get_or_create updates an existing device).
        # It sets the device's `name` only; a name a person gave the device in
        # Home Assistant lives in `name_by_user`, which this never touches.
        device = dev_registry.async_get_or_create(**self._hub_device_info())
        self.hub_device_id = device.id
        if not self._entered_into_device_registry:
            _LOGGER.debug("Entered Hub %s into the device registry", self.title)
            self._entered_into_device_registry = True

        await update_devices(self.hass, self.config_entry, self.api.rollers)
        self._async_update_title()

        async_dispatcher_send(
            self.hass, AUTOMATE_HUB_UPDATE.format(self.config_entry.entry_id)
        )

        for roller_id in list(self.current_rollers):
            if roller_id not in self.api.rollers:
                _LOGGER.debug("Notifying remove of %s", roller_id)
                self.current_rollers.pop(roller_id)
                # Scoped to this hub: the signal is process-wide, and another
                # hub may hold a motor with the same id.
                async_dispatcher_send(
                    self.hass,
                    AUTOMATE_ENTITY_REMOVE.format(self.shade_identifier(roller_id)),
                )
