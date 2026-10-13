"""The integration's actions: `automate.refresh_signal`."""

import asyncio
import logging

from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from . import const
from .const import DOMAIN, SERVICE_REFRESH_SIGNAL

_LOGGER = logging.getLogger(__name__)

# Standard targets only (entity_id, device_id, area_id, floor_id, label_id), at
# least one of them; nothing else is accepted.
REFRESH_SIGNAL_SCHEMA = cv.make_entity_service_schema({})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions, once."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_SIGNAL):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_SIGNAL, _async_refresh_signal, schema=REFRESH_SIGNAL_SCHEMA
    )


def _targeted_entity_ids(call: ServiceCall) -> list[str]:
    """Every entity the call targets, explicit ids first and in the order given.

    `entity_id: all` means every shade of every hub (their covers).
    """
    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        ent_reg = er.async_get(call.hass)
        return sorted(
            entry.entity_id
            for entry in ent_reg.entities.values()
            if entry.platform == DOMAIN and entry.domain == "cover"
        )
    selected = async_extract_referenced_entity_ids(call.hass, TargetSelection(call.data))
    selected.log_missing(set(), _LOGGER)  # devices, areas, floors or labels that do not exist
    targeted = selected.referenced | selected.indirectly_referenced
    explicit = cv.ensure_list(call.data.get(ATTR_ENTITY_ID) or [])
    ordered = list(dict.fromkeys([*explicit, *sorted(targeted)]))
    return [entity_id for entity_id in ordered if entity_id in targeted]


async def _async_refresh_signal(call: ServiceCall) -> None:
    """Query each targeted shade's live signal, one at a time, a little apart.

    A target may be any of a shade's entities (its cover, its Refresh signal
    button, a sensor); a shade targeted more than once is queried once. An
    entity that is not one of this integration's, a disconnected hub, an offline
    shade or an unreachable serial port is a warning in the log for that shade,
    never an exception out of the action: the other shades still get their turn.
    """
    hass = call.hass
    ent_reg = er.async_get(hass)
    hubs = hass.data.get(DOMAIN, {})

    queue = []  # (entity_id, hub, roller), one per shade, in target order
    seen = set()
    for entity_id in _targeted_entity_ids(call):
        entry = ent_reg.async_get(entity_id)
        hub = hubs.get(entry.config_entry_id) if entry and entry.platform == DOMAIN else None
        roller = hub.roller_for_unique_id(entry.unique_id) if hub else None
        if roller is None:
            _LOGGER.warning(
                "%s.%s: %s is not one of this integration's shade entities, skipped",
                DOMAIN,
                SERVICE_REFRESH_SIGNAL,
                entity_id,
            )
            continue
        if (id(hub), roller.id) in seen:
            continue
        seen.add((id(hub), roller.id))
        queue.append((entity_id, hub, roller))

    for index, (entity_id, hub, roller) in enumerate(queue):
        if index:
            await asyncio.sleep(const.REFRESH_SIGNAL_SPACING)
        try:
            replied = await hub.async_refresh_signal(roller)
        except HomeAssistantError as err:
            _LOGGER.warning("%s.%s: %s: %s", DOMAIN, SERVICE_REFRESH_SIGNAL, entity_id, err)
            continue
        if not replied:
            _LOGGER.warning(
                "%s.%s: %s (%s) did not answer the signal query",
                DOMAIN,
                SERVICE_REFRESH_SIGNAL,
                entity_id,
                roller.name,
            )
