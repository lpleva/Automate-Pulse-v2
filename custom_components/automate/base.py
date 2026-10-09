"""Base class for Automate Roller Blinds."""

import logging

from homeassistant.helpers import entity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.device_registry import async_get as get_dev_reg
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_registry import async_get as get_ent_reg

from .const import AUTOMATE_ENTITY_REMOVE, DOMAIN
from .vendor import aiopulse2

_LOGGER = logging.getLogger(__name__)


class AutomateBase(entity.Entity):
    """Base representation of an Automate roller."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    # None: the entity carries the device (roller) name alone. Sensors set their
    # own suffix ("Battery", "Signal") and HA prefixes the device name.
    _attr_name = None

    def __init__(self, roller: aiopulse2.Roller, config_entry_id: str):
        """Initialize the roller."""
        self.roller = roller
        self.config_entry_id = config_entry_id

    @property
    def available(self) -> bool:
        """Return True if roller and hub is available."""
        return self.roller.online and self.roller.hub.connected

    # pylint: disable=no-self-use
    def include_entity(self) -> bool:
        """Return True (default) if entity should be included.

        Overridden by superclasses.
        """
        return True

    async def async_remove_and_unregister(self):
        """Unregister from entity and device registry and call entity remove function."""
        _LOGGER.info("Removing %s %s", self.__class__.__name__, self.unique_id)

        ent_registry = get_ent_reg(self.hass)
        if self.entity_id in ent_registry.entities:
            ent_registry.async_remove(self.entity_id)

        dev_registry = get_dev_reg(self.hass)
        device = dev_registry.async_get_device_by_identifier(
            (DOMAIN, self.roller.id), self.config_entry_id
        )
        if device is not None:
            dev_registry.async_update_device(
                device.id, remove_config_entry_id=self.config_entry_id
            )

        await self.async_remove()

    async def async_added_to_hass(self):
        """Entity has been added to hass."""
        self.roller.callback_subscribe(self.notify_update)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                AUTOMATE_ENTITY_REMOVE.format(self.roller.id),
                self.async_remove_and_unregister,
            )
        )

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        self.roller.callback_unsubscribe(self.notify_update)

    async def notify_update(self, roller: aiopulse2.Roller):
        """Write updated device state information.

        A coroutine on purpose: the library runs plain functions in an executor
        thread, coroutines as tasks on the event loop, and state writes belong on
        the loop.
        """
        _LOGGER.debug(
            "Device update notification received: %s (%r)", roller.id, roller.name
        )
        self.async_write_ha_state()

    @property
    def unique_id(self):
        """Return the unique ID of this roller."""
        return self.roller.id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info, linking the roller to its hub."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self.roller.id)},
            name=self.roller.name,
            manufacturer="Rollease Acmeda",
            model=self.roller.devicetype,
            sw_version=self.roller.version,
        )
        # The link to the hub is a device registry ID (via_device_id); the
        # identifier-tuple via_device is deprecated and removed in HA 2027.8. Left
        # out when the hub device is not registered yet, because HA raises on an
        # unknown via_device_id and the platform then drops the entity.
        hub_device = get_dev_reg(self.hass).async_get_device_by_identifier(
            (DOMAIN, str(self.roller.hub.id)), self.config_entry_id
        )
        if hub_device is not None:
            info["via_device_id"] = hub_device.id
        return info
