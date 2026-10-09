"""Support for Automate Roller Blind Batteries."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_VOLTAGE,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .base import AutomateBase
from .const import AUTOMATE_HUB_UPDATE, DOMAIN
from .helpers import async_add_automate_entities


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Automate Rollers from a config entry."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    current_battery = set()
    current_signal = set()

    @callback
    def async_add_automate_sensors():
        async_add_automate_entities(
            hass, AutomateBattery, config_entry, current_battery, async_add_entities
        )
        async_add_automate_entities(
            hass, AutomateSignal, config_entry, current_signal, async_add_entities
        )

    hub.cleanup_callbacks.append(
        async_dispatcher_connect(
            hass,
            AUTOMATE_HUB_UPDATE.format(config_entry.entry_id),
            async_add_automate_sensors,
        )
    )


class AutomateBattery(AutomateBase, SensorEntity):
    """Representation of a Automate cover battery sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Battery"

    @property
    def native_value(self):
        """Return the battery percentage."""
        return self.roller.battery_percent

    @property
    def unique_id(self):
        """Return a unique identifier for this device."""
        return f"{self.roller.id}_battery"

    @property
    def extra_state_attributes(self):
        """Additional information about the battery state."""
        return {ATTR_VOLTAGE: self.roller.battery}

    def include_entity(self) -> bool:
        """Return True if roller has a battery."""
        return self.roller.has_battery


class AutomateSignal(AutomateBase, SensorEntity):
    """Representation of a Automate cover radio signal sensor.

    The value is dBm whichever path delivered it: the websocket reports dBm, and
    the library converts the serial protocol's raw register before storing it.
    """

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Signal"

    @property
    def native_value(self):
        """Return the signal strength in dBm."""
        return self.roller.signal

    @property
    def unique_id(self):
        """Return a unique identifier for this device."""
        return f"{self.roller.id}_signal"
