# Vendored `aiopulse2`

Upstream: [sillyfrog/aiopulse2](https://github.com/sillyfrog/aiopulse2), PyPI
package `aiopulse2`, **version 1.0.0**, taken from the `aiopulse2-1.0.0.tar.gz`
sdist on PyPI (sha256
`c84142919cc2b667c512f9c9ab42456165e66f862dd07d1240e72390427729c1`), vendored
2026-10-08. The sdist, the wheel and the upstream `v1.0.0` tag (`6d60e4b`) are
byte-identical in all four modules; upstream `main` (`e8b47c4`) differs only in
the `__version__` string. License: Apache 2.0, kept verbatim at
`aiopulse2/LICENSE`.

## Why it is vendored

The October 2026 audit of this integration found that most of its defects live
in the library rather than in the integration: the signal sensor flipping
between two scales, the one-shot serial name lookup that leaves Home Assistant
with no entities at all when the hub is slow to answer, the websocket being
torn down by any message without a `shades` object, missing keys being read as
"offline and closed", and the 2 s hard-coded polling of the hub. The library
is unmaintained enough (one release in 2025, no tests) that shipping a patched
copy is the only way to run those fixes. The PyPI dependency was dropped from
`manifest.json`; the library's own `websockets` requirement is declared there
instead, and `async_timeout` is no longer needed (stdlib `asyncio.timeout`).

The package needed no import rewrite: it already used relative imports.

## Changes made (behaviour changes, all covered by `test/test_library_*.py`)

`devices.py`:

1. **Signal scale.** `Roller.set_signal` converts the serial `,R<hex>` register
   to dBm (`R/2 - 134`, so `R68` is -82 dBm) instead of storing the raw integer
   (104). The websocket `rs` field is already dBm, so the two paths now agree.
   `set_signal` returns whether the value changed.
2. **Live signal sweep (opt-in).** `Hub(signal_poll_interval=N)` (N is 0 or at
   least 10) queries ONE shade's position and signal over the serial port
   (`!<id>r?;`) every N seconds, round-robin, skipping ticks while a name lookup
   still owns the port. On the Pulse Pro the websocket `rs` is the hub's cached
   last reading and does not refresh by itself; this query polls the motor live
   over RF. Each roller keeps two timestamps, `serial_signal_at` and
   `serial_position_at`, set by every live reply. While the sweep is on and a
   timestamp is fresh (within three sweep rounds, `signal_stale_after()`), the
   cached `rs` (`use_ws_signal`) or `mp` (`use_ws_position`) is not applied to
   that roller. `Roller.move_to` and `move_stop` clear `serial_position_at`, so
   the hub's position reports for a move commanded through this library apply at
   once. Including position rests on an unverified field observation (cached
   position looking stale after remote moves); signal is the proven case.
   Default 0 (off): with it off the library behaves as upstream, cached values
   only.
3. **Serial name lookup is retried.** Upstream's 3 s read timeout exited both
   loops (its "sleep 10 and retry" was unreachable) and never closed the socket;
   a reply for an unknown id raised `KeyError` and killed the runner; nothing
   ever started it again. Now: each session opens with a 10 s connect timeout
   (`SERIAL_CONNECT_TIMEOUT`, so a dead port cannot hold the lock), reads until
   3 s of quiet or EOF and closes its socket; a failed attempt, whether a
   connection error or any other exception (the `except` is inside the loop),
   is counted and retried with a doubling back-off (10 s to 5 min); after three
   failed attempts `_apply_fallback_names` runs regardless of what the rollers
   are called: still-nameless rollers get the placeholder `Shade <id>`,
   `unknown_rollers` is cleared and consumers are released. The heartbeat's
   hourly tick (`requeue_placeholder_names`) re-queues every roller whose name
   is a placeholder or still None and starts the runner again; against a port
   that stays silent that run also ends after three attempts, so the sweep is
   never starved. Replies for unknown ids are ignored. The name lookup and the
   sweep share one `asyncio.Lock`, so port 1487 is never opened twice at once.
4. **Defensive parsing.** `wsconsumer` tolerates a shadow without `shades`
   (upstream raised `KeyError`, which tore the websocket down and reconnected 10
   s later, for every such message), non-dict `mfi`/`firmware`/shade entries,
   and unusable `mp` values; one bad shade no longer drops the whole message.
   Only keys the hub actually sent are applied: upstream defaulted `ol` to False
   and `mp` to 100, so a sparse update marked a shade offline and closed.
5. **Truncated JSON (Pulse Pro).** The brace-repair is kept, but when the cut
   fell INSIDE a shade's own object (shades container and a shade both
   unterminated) that last shade is skipped for the poll, since its final value
   may be partial. Unparseable messages log one WARNING per connection, then
   debug (upstream: ERROR every 2 s). "Unknown WS response" is debug, not info.
6. **Polling interval.** `Hub(poll_interval=N)` (minimum 2, default 10; upstream
   hard-coded 2). The hourly details-reset counter follows the interval.
7. **Reconnect back-off.** 5 s doubling to 60 s instead of a fixed 10 s; reset
   on a successful connection; a clean close by the hub also reconnects;
   `handshake` is cleared on every disconnect so commands cannot be sent into a
   dead socket.
8. **Motor type is a hub-level change.** The first time a roller's `vo` arrives
   (so `devicetypeshort`, and with it `has_battery`, becomes known) the hub
   callbacks fire, so a consumer can create the entities that depend on it.
9. **Task hygiene.** The heartbeat, serial and sweep tasks are stored and
   cancelled by `run()` on exit and by `test()` in a `finally`; `test()` resets
   `running`, `ws`, `connected` and `handshake` whatever happens, and raises
   `InvalidResponseException` if the socket closes before a valid shadow.
10. **`send_payload`** waits at most `SEND_HANDSHAKE_TIMEOUT` (10 s) for the
    websocket and raises `NotConnectedException` instead of hanging the caller;
    a failed send raises too.
11. **Serial position replies** notify roller subscribers when a value changed
    (upstream wrote the attributes silently).
12. `asyncio.get_running_loop()` is preferred (falls back to `get_event_loop()`
    outside a loop); `inspect.iscoroutinefunction` replaces the asyncio one
    (deprecated in Python 3.14, removed in 3.16); `asyncio.timeout` replaces
    `async_timeout`; the TLS context gets a TLS 1.2 floor.
13. **Heartbeat guard.** The poll loop's body is wrapped: `CancelledError` is
    re-raised, any other exception is logged as a warning and the loop keeps
    polling, since it is the only way state reaches consumers.
14. **Missed signal checks are visible (1.2.0).** Each roller counts the
    sweep's checks and misses (`signal_checks`, `signal_misses`, both
    incremented in `_serial_query_position`): a check is a query whose serial
    session opened, a miss is one that ended without that roller's reply. A
    session that could not open at all (`OSError`, connect timeout) counts
    neither, since that is the port, not the shade. `signal_missed` records
    whether the latest check missed, `signal_stale` is set by
    `expire_stale_signal_readings` (every sweep tick) once the last reading is
    older than `signal_stale_after()`, and a reply clears both. The new
    `Roller.signal_reading` is what a consumer shows: with the sweep off it is
    `signal` (the cached `rs`, as upstream); with it on it is the latest live
    reading, or None before the first reading, after a miss, or once stale.
    `use_ws_signal` is now simply "sweep off": with the sweep on the cached
    `rs` is never applied, not even after the stale window (until 1.1.0 it came
    back then, so a dead port showed an old number as current). Position
    (`use_ws_position`) is unchanged. Every check notifies the roller's
    subscribers, so the counters reach consumers.

`__init__.py`: exports `HubBaseException`; `__version__` is `1.0.0+lpleva`.
`const.py`, `errors.py`: unchanged.

## What was audited and deliberately NOT changed

- **TLS verification is off** (`CERT_NONE`, `check_hostname = False`). The hub
  presents a self-signed certificate, so there is no CA to verify against. The
  context is used for exactly one connection, the websocket to the configured
  hub host, and is never installed as a default. Pinning the hub's certificate
  (trust on first use) would close the remaining gap; it needs the certificate
  from the hub, which this audit could not fetch.
- **The hub has no authentication** on either port, and the serial port accepts
  movement commands (`o`, `c`, `m<pct>`). That is the hub's design; the library
  neither adds nor could add protection. Keep the hub on a trusted VLAN.
- **Payloads that write to the hub.** Exactly three `desired` writes exist:
  `movePercent` and `stopShade`, sent only from `Roller.move_to` / `move_stop`
  (user commands), and `{"query": True}`, queued once per roller per hour at
  most when its shadow entry lacks `vo`. The query asks the hub to poll that
  motor for voltage and version; it does not move anything. The heartbeat's
  `{"method": "shadow", "src": "app", "id": ...}` is a read.
- The battery percentage curves (8.3 V and 12 V, split at 9.0 V) are upstream's
  heuristic, discontinuous at the split; left as is for want of real data.

## Re-applying on a newer upstream

Diff upstream's new release against the `v1.0.0` tag, then re-apply the
fourteen changes above on top; the tests in `test/test_library_*.py` are the
acceptance suite. Upstream `main` has no tests of its own.
