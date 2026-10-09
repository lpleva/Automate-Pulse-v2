"""Vendored aiopulse2: message parsing, signal scale, back-off arithmetic.

Pure asyncio, no network. Every test here pins a fix from the October 2026 audit.
"""

import asyncio
import json
import logging
import time

import pytest

from custom_components.automate.vendor.aiopulse2 import devices
from custom_components.automate.vendor.aiopulse2.devices import Hub

from .conftest import HUB_HOST, HUB_ID, shadow_message


@pytest.fixture
def hub():
    """A hub with callbacks delivered immediately (no name gating)."""
    return Hub(HUB_HOST, delay_callbacks=False)


# --- signal scale -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "dbm"),
    [("68", -82), ("54", -92), ("52", -93), ("69", -81.5), (104, -82)],
)
def test_serial_signal_to_dbm(raw, dbm):
    """",R<hex>" is a raw register: dBm = R/2 - 134 (R54, R52 seen on the kitchen shade)."""
    assert devices.serial_signal_to_dbm(raw) == dbm


def test_serial_signal_to_dbm_rejects_garbage():
    assert devices.serial_signal_to_dbm("zz") is None
    assert devices.serial_signal_to_dbm(None) is None


async def test_signal_does_not_flip_between_scales(hub):
    """The audit's headline bug: ws "rs" -82 then serial R68 must both read -82."""
    assert hub.signal_poll_interval == 0  # sweep off by default: ws value always applies
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    roller = hub.rollers["ABC"]
    assert roller.signal == -82
    hub.response_parse("!ABCr050b000,R68;")
    assert roller.signal == -82, "upstream stored 104 here"
    assert roller.closed_percent == 50
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    assert roller.signal == -82


async def test_live_serial_signal_wins_over_cached_ws_value():
    """With the sweep on, the live serial reading is kept over the hub's cached rs."""
    hub = Hub(HUB_HOST, delay_callbacks=False, signal_poll_interval=30)
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    hub.response_parse("!ABCr050b000,R54;")  # live: -92 dBm
    assert hub.rollers["ABC"].signal == -92
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    assert hub.rollers["ABC"].signal == -92, "cached rs overwrote the live reading"


async def test_cached_ws_signal_returns_when_serial_reading_is_stale():
    hub = Hub(HUB_HOST, delay_callbacks=False, signal_poll_interval=30)
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    hub.response_parse("!ABCr050b000,R54;")
    roller = hub.rollers["ABC"]
    roller.serial_signal_at = time.monotonic() - hub.signal_stale_after() - 1
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    assert roller.signal == -82


async def test_serial_position_response_notifies_on_change(hub):
    seen = []
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "mp": 10}}))
    hub.rollers["ABC"].callback_subscribe(lambda r: seen.append(r.signal))
    hub.response_parse("!ABCr050b000,R54;")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [-92]
    hub.response_parse("!ABCr050b000,R54;")  # identical: no callback
    await asyncio.sleep(0)
    assert seen == [-92]


# --- back-off ---------------------------------------------------------------------


def test_next_backoff_doubles_and_clamps():
    seq = []
    d = 0.0
    for _ in range(6):
        d = devices.next_backoff(d, 5, 60)
        seq.append(d)
    assert seq == [5, 10, 20, 40, 60, 60]


# --- truncated JSON (Pulse Pro) ---------------------------------------------------


def _msg(shades, trailing=None):
    reported = {"hubId": HUB_ID, "shades": shades}
    if trailing is not None:
        reported.update(trailing)
    return json.dumps({"result": {"reported": reported}})


def test_repair_missing_final_brace_keeps_every_shade():
    """The Pro drops exactly the final brace (aiopulse2 #15): nothing is suspect."""
    full = _msg({"A": {"rs": -82}, "B": {"rs": -70}})
    parsed, suspect = devices._repair_truncated_json(full[:-1])
    assert parsed["result"]["reported"]["shades"] == {"A": {"rs": -82}, "B": {"rs": -70}}
    assert suspect is False


def test_repair_missing_two_braces_shades_last_member():
    """shades is the last member and both its brace and the outer one are gone."""
    full = _msg({"A": {"rs": -82}, "B": {"rs": -70}})
    parsed, suspect = devices._repair_truncated_json(full[:-2])
    assert parsed["result"]["reported"]["shades"]["B"] == {"rs": -70}
    assert suspect is False


def test_repair_cut_inside_a_shade_flags_it():
    full = _msg({"A": {"rs": -82}, "B": {"rs": -70}})
    cut = full[: full.index('"B": {"rs": -70}') + len('"B": {"rs": -70')]
    parsed, suspect = devices._repair_truncated_json(cut)
    assert parsed is not None
    assert suspect is True


def test_repair_gives_up_on_a_cut_inside_a_value():
    full = _msg({"A": {"rs": -82}})
    cut = full[: full.index("-82") + 1]  # ends in "-"
    assert devices._repair_truncated_json(cut) == (None, False)


async def test_wsconsumer_skips_the_shade_cut_in_half(hub):
    await hub.wsconsumer(_msg({"A": {"rs": -82, "mp": 20}, "B": {"rs": -70, "mp": 100}}))
    assert hub.rollers["B"].closed_percent == 100
    full = _msg({"A": {"rs": -82, "mp": 20}, "B": {"rs": -70, "mp": 10}})
    cut = full[: full.rindex("10") + 1]  # ends in `"mp": 1`: a repaired, WRONG value
    assert cut.endswith('"mp": 1')
    await hub.wsconsumer(cut)
    assert hub.rollers["A"].signal == -82
    assert hub.rollers["B"].closed_percent == 100, "partial shade must be ignored"


async def test_live_position_wins_over_cached_ws_value_until_stale():
    hub = Hub(HUB_HOST, delay_callbacks=False, signal_poll_interval=30)
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "mp": 30}}))
    roller = hub.rollers["ABC"]
    hub.response_parse("!ABCr050b000,R54;")  # live: 50 % closed
    assert roller.closed_percent == 50
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "mp": 30}}))
    assert roller.closed_percent == 50, "the cached position flapped the live one back"
    roller.serial_signal_at = time.monotonic() - hub.signal_stale_after() - 1
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "mp": 30}}))
    assert roller.closed_percent == 30


async def test_unparseable_message_warns_once_then_debug(hub, caplog):
    caplog.set_level(logging.DEBUG)
    bad = '{"result": {"reported": {"shades": {"A": {"rs": -'  # braces cannot save this
    await hub.wsconsumer(bad)
    await hub.wsconsumer(bad)
    await hub.wsconsumer(bad)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert hub.json_failures == 3


# --- defensive parsing ------------------------------------------------------------


async def test_missing_keys_mean_no_news_not_offline(hub):
    """Upstream defaulted ol=False and mp=100: a sparse update closed and offlined a shade."""
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "mp": 30, "is": True}}))
    roller = hub.rollers["ABC"]
    assert (roller.online, roller.closed_percent, roller.moving) == (True, 30, False)
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -80}}))
    assert (roller.online, roller.closed_percent, roller.moving) == (True, 30, False)
    assert roller.signal == -80


async def test_shadow_without_shades_does_not_raise(hub):
    msg = json.dumps({"result": {"reported": {"hubId": HUB_ID, "name": "x"}}})
    await hub.wsconsumer(msg)  # upstream: KeyError -> websocket torn down
    assert hub.id == HUB_ID
    assert hub.connected is True


async def test_odd_shapes_do_not_raise(hub):
    msg = json.dumps(
        {
            "result": {
                "reported": {
                    "hubId": HUB_ID,
                    "mfi": "not a dict",
                    "firmware": None,
                    "shades": {"ABC": {"mp": "garbage", "rs": -82}, "BAD": "string"},
                }
            }
        }
    )
    await hub.wsconsumer(msg)
    assert hub.rollers["ABC"].signal == -82
    assert hub.rollers["ABC"].closed_percent is None
    assert "BAD" not in hub.rollers
    await hub.wsconsumer("[1, 2, 3]")
    await hub.wsconsumer(json.dumps({"result": "nope"}))
    await hub.wsconsumer(json.dumps({"error": {"code": -1}}))


async def test_learning_the_motor_type_is_a_hub_change(hub):
    """So the integration gets a hub update and can add the battery sensor."""
    hub_updates = []
    hub.callback_subscribe(lambda h: hub_updates.append("hub"))
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True}}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(hub_updates) == 2  # connected, then the new roller
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "vo": "12.1D23"}}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(hub_updates) == 3, "learning the motor type must notify the hub level"
    roller = hub.rollers["ABC"]
    assert roller.has_battery and roller.battery == 12.1 and roller.version == "23"
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82, "ol": True, "vo": "12.0D23"}}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(hub_updates) == 3, "a later voltage change is roller-level only"


async def test_details_query_payload_is_queued_once_per_roller(hub):
    """The only non-command write to the hub: ask it to poll a motor for details."""
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
    await hub.wsconsumer(shadow_message({"ABC": {"rs": -82}}))
    assert hub.payload_queue == [
        {
            "method": "shadow",
            "args": {
                "desired": {"shades": {"ABC": {"query": True}}},
                "timeStamp": hub.payload_queue[0]["args"]["timeStamp"],
            },
        }
    ]
    assert hub.sent_details_request == {"ABC"}


async def test_serial_responses_for_unknown_ids_are_ignored(hub):
    hub.response_parse("!ZZZr050b000,R68;")  # upstream: KeyError killed the runner
    hub.response_parse("!ZZZNAMEGhost;")
    hub.response_parse("!000NAMEHub;")
    assert hub.rollers == {}


def test_hub_poll_interval_floor_and_sweep_off():
    h = Hub(HUB_HOST, poll_interval=0.5, signal_poll_interval=0)
    assert h.heartbeatinterval == devices.MIN_POLL_INTERVAL
    assert h.signal_poll_interval == 0
    h = Hub(HUB_HOST, poll_interval=60, signal_poll_interval=3)
    assert h.heartbeatinterval == 60
    assert h.signal_poll_interval == devices.MIN_SIGNAL_POLL_INTERVAL
