"""Download diagnostics: what the integration holds in memory, redacted.

Read-only. Nothing here talks to the hub: every value is what the library has
already received over the websocket (the shadow) or the serial port (the live
readings), plus the integration's own settings and counters. Home Assistant
serves it as a JSON file from Settings, Devices & services, Automate Pulse Hub
v2, the three-dot menu, Download diagnostics; the same per shade from the
shade's device page.

The hub's shadow carries fields this integration does not use, and some of
them a user would not want in a file they attach to a bug report (the hub's
Wi-Fi details, cloud tokens, where the house is). Two guards, in this order:
only the fields named in SHADE_SHADOW_KEYS and HUB_SHADOW_KEYS pass through
with their values; every other field keeps its name and gets a marker with
its type and size only ("<unknown field: str, 24 chars>"), so a field nobody
has seen yet can never leak. Then TO_REDACT runs over the whole output, at any
depth, for sensitive keys inside the fields that did pass (the firmware and
Matter objects are dicts). The hub's MAC and LAN address stay: both are needed
to tell two hubs apart and neither leaves the LAN.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .hub import PulseHub
from .vendor import aiopulse2
from .vendor.aiopulse2 import const as pulse_const

# Keys redacted wherever they appear, at any depth, in the hub's shadow or in
# anything else this file reports. Exact, case-sensitive matches (that is how
# async_redact_data works), so the common spellings are all listed.
TO_REDACT = {
    # Wi-Fi and other credentials
    "ssid",
    "SSID",
    "wifi",
    "wifiSSID",
    "wifiPassword",
    "password",
    "pass",
    "passwd",
    "psk",
    "pin",
    "passcode",
    "setupCode",
    "pairingCode",
    "manualCode",
    "qrCode",
    # Tokens, keys, certificates
    "jwt",
    "token",
    "accessToken",
    "refreshToken",
    "idToken",
    "apiKey",
    "key",
    "secret",
    "clientSecret",
    "cert",
    "certificate",
    "privateKey",
    # Where the house is
    "lat",
    "lon",
    "lng",
    "latitude",
    "longitude",
    "location",
    "geo",
    "timezone",
    "timeZone",
    "tz",
    "address",
    # Identity: the hub's serial number and whoever's cloud account it is tied to
    "serial",
    "serialNumber",
    "sn",
    "SN",
    "email",
    "owner",
    "ownerId",
    "userId",
    "user",
    "account",
    "cloudId",
    "thingName",
}

# Shadow fields passed through with their values. Anything else is shown as a
# marker: its name, type and size, never its value.
SHADE_SHADOW_KEYS = {"mp", "rs", "ol", "vo", "ta", "is", "ls"}
HUB_SHADOW_KEYS = {"hubId", "mac", "name", "firmware", "mfi", "matter", "onlineLatest"}

# Each shade's entities, by the unique-id suffix the platforms mint.
_SHADE_ENTITIES = (
    ("cover", "cover", ""),
    ("battery", "sensor", "_battery"),
    ("signal", "sensor", "_signal"),
    ("missed_signal_checks", "sensor", "_signal_misses"),
    ("refresh_signal", "button", "_refresh_signal"),
)


def _age(now: float, at: float | None) -> float | None:
    """Seconds since a time.monotonic() stamp, to a tenth, or None without one."""
    if at is None:
        return None
    return round(now - at, 1)


def _unknown_marker(value: Any) -> str:
    """What an unlisted shadow field is shown as: its type and size, never its value."""
    if isinstance(value, str):
        return f"<unknown field: str, {len(value)} chars>"
    if isinstance(value, dict):
        return f"<unknown field: dict, {len(value)} keys>"
    if isinstance(value, list):
        return f"<unknown field: list, {len(value)} items>"
    return f"<unknown field: {type(value).__name__}>"


def _allowed_shadow(shadow: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any] | None:
    """The shadow with only the allowed fields' values; the rest as markers."""
    if shadow is None:
        return None
    return {k: v if k in allowed else _unknown_marker(v) for k, v in shadow.items()}


def _decode_vo(vo: Any) -> dict[str, Any] | None:
    """The shadow's "vo" string ("12.1D23") taken apart: volts, type code, motor firmware."""
    if vo is None:
        return None
    match = pulse_const.WS_ROLLER_VOLTAGE.match(str(vo))
    if not match:
        return {"raw": vo, "decoded": False}
    code = match.group("type")
    return {
        "raw": vo,
        "decoded": True,
        "volts": float(match.group("voltage")),
        "device_type_code": code,
        "device_type": pulse_const.TYPES.get(code, f"unknown {code}"),
        "motor_firmware_version": match.group("version"),
    }


def _entry_section(entry: ConfigEntry, hub: PulseHub) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "unique_id": entry.unique_id,
        "data": dict(entry.data),
        "options": dict(entry.options),
        "options_at_setup": dict(hub.options_at_setup),
    }


def _hub_section(hub: PulseHub, now: float) -> dict[str, Any]:
    """The hub as the library sees it: identity, connection, polling, serial, shadow."""
    api = hub.api
    shadow = api.last_shadow_hub
    sweep_on = api.signal_poll_interval > 0
    ws_state = getattr(api.ws, "state", None)
    last_error = api.lasterrorlog
    return {
        "id": api.id,
        "name": api.name,
        "host": api.host,
        "mac": api.mac_address,
        "model": api.model,
        "firmware_version": api.firmware_ver,
        "matter": shadow.get("matter") if shadow else None,
        "online_latest": shadow.get("onlineLatest") if shadow else None,
        "library_version": aiopulse2.__version__,
        "connection": {
            "running": api.running,
            "connected": api.connected,
            "websocket_open": ws_state is not None and ws_state.name == "OPEN",
            "handshake": api.handshake.is_set(),
            "last_error": getattr(last_error, "__name__", None)
            if last_error is not None
            else None,
            "unparseable_messages": api.json_failures,
        },
        "polling": {
            "shadow_poll_interval_s": api.heartbeatinterval,
            "signal_poll_interval_s": api.signal_poll_interval,
            "live_sweep_on": sweep_on,
            "live_reading_stale_after_s": api.signal_stale_after() if sweep_on else None,
            "sweep_next_index": api._sweep_position,
            "pending_payloads": len(api.payload_queue),
            "details_requested_for": sorted(api.sent_details_request),
        },
        "serial_protocol": {
            "port": api.serial_port,
            "name_lookup_ok": api.serialok,
            "name_lookup_running": api.serialrunning,
            "last_error": api.lastserialerror,
            "unnamed_shades": sorted(api.unknown_rollers),
            "sessions_opened": api.serial_sessions,
            "open_failures": api.serial_open_failures,
            "seconds_since_last_session": _age(now, api._serial_session_ended_at),
            "queries_in_flight": sorted(api._signal_queries),
        },
        "shadow": _allowed_shadow(shadow, HUB_SHADOW_KEYS),
        "shadow_age_s": _age(now, api.last_shadow_at),
    }


def _shade_section(
    roller: aiopulse2.Roller, ent_reg: er.EntityRegistry, now: float
) -> dict[str, Any]:
    """One shade: identity, entities, position, signal, battery, motor, and its shadow entry."""
    hub = roller.hub
    shadow = roller.last_shadow
    closed = roller.closed_percent
    return {
        "id": roller.id,
        "name": roller.name,
        "name_is_placeholder": roller.name_is_placeholder,
        "entities": {
            label: ent_reg.async_get_entity_id(domain, DOMAIN, f"{roller.id}{suffix}")
            for label, domain, suffix in _SHADE_ENTITIES
        },
        "online": roller.online,
        "position": {
            "closed_percent": closed,
            "cover_position": 100 - closed if closed is not None else None,
            "target_closed_percent": roller.target_closed_percent,
            "tilt_percent": roller.tilt_percent,
            "moving": roller.moving,
            "action": roller.action.name,
            "live_reading_age_s": _age(now, roller.serial_position_at),
            "live_reading_in_use": not hub.use_ws_position(roller),
        },
        "signal": {
            "dbm": roller.signal,
            "sensor_reading_dbm": roller.signal_reading,
            "live_reading_age_s": _age(now, roller.serial_signal_at),
            "live_reading_fresh": hub._live_reading_is_fresh(roller.serial_signal_at),
            "last_check_missed": roller.signal_missed,
            "stale": roller.signal_stale,
            "checks": roller.signal_checks,
            "misses": roller.signal_misses,
        },
        "battery": {
            "has_battery": roller.has_battery,
            "voltage": roller.battery,
            "percent": roller.battery_percent,
        },
        "motor": {
            "device_type_code": roller.devicetypeshort,
            "device_type": roller.devicetype,
            "firmware_version": roller.version,
            "vo": _decode_vo(shadow.get("vo")) if shadow else None,
        },
        "shadow": _allowed_shadow(shadow, SHADE_SHADOW_KEYS),
        "shadow_age_s": _age(now, roller.last_shadow_at),
    }


def _diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, roller_ids: list[str] | None = None
) -> dict[str, Any]:
    """The entry, the hub, and the shades named (None: every shade), redacted."""
    hub: PulseHub = hass.data[DOMAIN][entry.entry_id]
    now = time.monotonic()
    data: dict[str, Any] = {"entry": _entry_section(entry, hub)}
    if hub.api is None:
        data["hub"] = None
        data["shades"] = {}
        return async_redact_data(data, TO_REDACT)
    ent_reg = er.async_get(hass)
    data["hub"] = _hub_section(hub, now)
    data["shades"] = {
        roller_id: _shade_section(roller, ent_reg, now)
        for roller_id, roller in hub.api.rollers.items()
        if roller_ids is None or roller_id in roller_ids
    }
    return async_redact_data(data, TO_REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """The hub and every shade it knows."""
    return _diagnostics(hass, entry)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """The hub alone (plus the shade ids) for the hub's device; the hub and that shade for a shade's."""
    hub: PulseHub = hass.data[DOMAIN][entry.entry_id]
    ours = [ident for domain, ident in device.identifiers if domain == DOMAIN]
    if not ours:
        return {
            "error": "not one of this integration's devices",
            "identifiers": [list(i) for i in device.identifiers],
        }
    ident = ours[0]
    if hub.api is not None and ident == str(hub.api.id):
        data = _diagnostics(hass, entry, roller_ids=[])
        data["shade_ids"] = sorted(hub.api.rollers)
        return data
    return _diagnostics(hass, entry, roller_ids=[ident])
