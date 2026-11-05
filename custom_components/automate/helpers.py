"""Helper functions for Automate Pulse."""

import logging

from homeassistant.core import callback
from homeassistant.helpers.device_registry import async_get as get_dev_reg

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def entry_scope(config_entry) -> str:
    """The prefix that makes this entry's shade ids unique across hubs.

    A motor id ("M4G") is three characters the HUB assigns at pairing, so two
    hubs can both hold an "M4G". Every per-shade unique id and device
    identifier is therefore `<scope>_<motor id>`, where the scope is the
    entry's unique id: the hub's own id, which the config flow records. An
    entry without one (a hub that never reported an id when it was added)
    falls back to its entry id, which is just as unique and just as stable.
    The migration in `__init__.py` uses the same function, so what it writes
    and what the entities mint always agree.
    """
    return config_entry.unique_id or config_entry.entry_id


def shade_identifier(scope: str, roller_id: str) -> str:
    """The id of one shade under one hub: the cover's unique id and the device identifier."""
    return f"{scope}_{roller_id}"


@callback
def async_add_automate_entities(
    hass, entity_class, config_entry, current, async_add_entities
):
    """Add any new entities.

    A roller is only recorded in `current` once its entity was actually added, so
    an entity whose include_entity() is not yet satisfiable (the battery sensor
    before the hub has told us the motor type) is retried on the next hub update.
    """
    hub = hass.data[DOMAIN][config_entry.entry_id]
    _LOGGER.debug("Looking for new %s on: %s", entity_class.__name__, hub.host)

    api = hub.api.rollers

    new_items = []
    for unique_id, roller in api.items():
        if unique_id not in current:
            new_item = entity_class(roller, config_entry)
            if new_item.include_entity():
                _LOGGER.debug("New %s %s", entity_class.__name__, unique_id)
                current.add(unique_id)
                new_items.append(new_item)

    if new_items:
        async_add_entities(new_items)


async def update_devices(hass, config_entry, api):
    """Tell hass that device info has been updated."""
    dev_registry = get_dev_reg(hass)
    scope = entry_scope(config_entry)

    for api_item in api.values():
        # Update Device name. By-identifier-within-this-entry lookup: the plain
        # async_get_device is deprecated (identifiers are no longer unique across
        # config entries) and stops working in HA 2027.8.
        device = dev_registry.async_get_device_by_identifier(
            (DOMAIN, shade_identifier(scope, api_item.id)), config_entry.entry_id
        )
        if device is not None and api_item.name:
            dev_registry.async_update_device(
                device.id,
                name=api_item.name,
            )
