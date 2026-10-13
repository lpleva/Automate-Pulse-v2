"""Shared fixtures for the Automate Pulse Hub v2 tests.

No test opens a network connection: the websocket runner and the serial name
lookup are patched out, and the fake serial servers listen on 127.0.0.1 only.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.automate.const import DOMAIN
from custom_components.automate.vendor.aiopulse2 import devices

pytest_plugins = "pytest_homeassistant_custom_component"

# TEST-NET-1 (RFC 5737): never routable, so even an un-patched call cannot reach
# a real hub.
HUB_HOST = "192.0.2.10"
HUB_ID = "2001234"
HUB_MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return


async def _fake_run(self):
    """Stand in for Hub.run: no websocket, just mark running until cancelled."""
    self.running = True
    try:
        await asyncio.Event().wait()
    finally:
        self.running = False


@pytest.fixture(autouse=True)
def no_serial_spacing(monkeypatch):
    """No 0.75 s pause between fake serial sessions; the spacing test sets it back."""
    monkeypatch.setattr(devices, "SERIAL_SESSION_SPACING", 0)


@pytest.fixture
def no_network():
    """Keep the library off the network: no websocket, no serial lookups."""
    with (
        patch.object(devices.Hub, "run", _fake_run),
        patch.object(devices.Hub, "runserial", AsyncMock()),
        patch.object(devices.Hub, "requeue_placeholder_names", lambda self: None),
    ):
        yield


@pytest.fixture
def config_entry(hass):
    """A configured hub entry, not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test hub",
        data={"host": HUB_HOST},
        unique_id=HUB_ID,
    )
    entry.add_to_hass(hass)
    return entry


def shadow_message(shades, *, hub_id=HUB_ID, name="Living room hub", **extra):
    """Build a hub shadow response as the websocket delivers it."""
    reported = {
        "hubId": hub_id,
        "name": name,
        "mac": HUB_MAC,
        "firmware": {"version": "1.3.0", "RFversion": "1.0"},
        "matter": {"vendorID": 4938, "productID": 1},
        "shades": shades,
    }
    reported.update(extra)
    return json.dumps({"id": 1, "src": "hub", "result": {"reported": reported}})


async def settle(hass):
    """Let the library's loop tasks and HA's own tasks finish."""
    for _ in range(3):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def feed(hass, hub, shades, names=None, **extra):
    """Push one shadow update, answer the name lookup, and settle.

    `names` maps roller id to the name the serial port would return; a roller
    without one keeps whatever name it already has.
    """
    await hub.api.wsconsumer(shadow_message(shades, **extra))
    for rollerid, name in (names or {}).items():
        hub.api.response_parse(f"!{rollerid}NAME{name};")
    await settle(hass)
