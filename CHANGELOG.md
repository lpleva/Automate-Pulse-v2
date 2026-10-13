# Changelog

This is Lukas Pleva's fork of
[sillyfrog/Automate-Pulse-v2](https://github.com/sillyfrog/Automate-Pulse-v2).
It carries the October 2026 audit fixes upstream does not have; a Saturday job
merges upstream into it and the delta is audited before it reaches `main`.

## Unreleased

A live signal reading on demand, for one shade or several, without waiting for
the sweep to come round (at 30 s with eight shades, up to four minutes).

### Added

- **`Refresh signal` button per shade** (`button.<shade>_refresh_signal`,
  diagnostic, so it stays off auto-generated dashboards; same device as the
  shade). Pressing it sends that one shade, right away, exactly the query the
  live sweep sends on its turn (the serial `!<id>r?;` on port 1487, which makes
  the hub poll the motor over radio), and the shade's Signal sensor updates when
  the hub answers, through the same path as a sweep reply. It never moves a
  shade, changes a limit, or sends any other command. If the hub is
  disconnected, the shade is offline or no longer known to the hub, or the
  hub's serial port cannot be reached, the press fails with a message that says
  which; a shade that does not answer within 8 s fails with "did not answer",
  and counts as a missed check while the sweep is on. Pressing it six times in
  a row sends one query: presses that arrive while the shade's query is still
  pending share its answer.
- **`automate.refresh_signal` action**: the same query for every targeted shade,
  one after another, so a card or an automation can refresh several at once.
  Targets are the standard ones: a shade's cover or its Refresh signal button
  (any of its sensors works too), a device, an area, or `entity_id: all`. A
  shade targeted more than once is queried once. An explicitly named entity
  that is not one of this integration's shades is a warning; the other
  entities of a targeted area or device (the room's lights) are skipped
  silently, as Home Assistant's own entity actions do. A disconnected hub, an
  offline shade, an unreachable port or a shade that does not answer is a
  warning in the log for that shade, never an error out of the action: the
  other shades still get their turn.
- **Serial sessions are spaced, whoever asks.** The library now opens the hub's
  serial port no sooner than 0.75 s after the previous session closed, under
  the same lock that already kept it from being opened twice at once. That
  covers a burst of button presses on different shades, the action's targets,
  the sweep and the name lookup alike, so the hub's radio is never asked twice
  in quick succession. A press queued behind a sweep session in progress waits
  for it (up to 8 s) and then its 0.75 s.
- With the sweep off (option at 0) both still work: the live reading shows until
  the next hub poll puts the hub's cached value back, as 1.1.0 documented for a
  serial reading.

### Testing

- 77 tests (68 before): the button exists per shade with the diagnostic category,
  a stable unique id and the shade's device; a press sends exactly that shade's
  query and nothing else through the fake serial port, and the sensor follows the
  reply; six presses at once open one session; each error case (hub
  disconnected, shade offline, dead port, silent shade, shade no longer known)
  raises a clear message; the action queries the targets in the order given,
  once per shade, resolves a device target, an area target (with a light and a
  lock in the same area, queried without a warning) and `all`, skips unknown
  explicit entities with a warning, refuses an empty call, and warns per shade
  on a disconnected hub without opening the port; and in the library,
  `query_signal` reports a reply and a miss, six callers at once share one
  session, and sessions are spaced 0.75 s apart against a fake clock (a full
  wait, a remainder, and no wait once enough time has passed).

## 1.2.0 (2026-10-09)

Missed live signal checks are visible instead of hidden. Until now, with the
live sweep on, a shade that did not answer its serial query (8 s of silence)
kept showing its previous number, and after three sweep rounds the hub's
cached `rs` came back in, so a shade that had not been heard from in an hour
looked exactly like one read a minute ago. That is the wrong way round for
the thing the sweep exists for: finding the shades with unstable signal.

### Changed

- **The signal sensor reads `unknown` when there is no current live reading**
  (sweep on only). That is: the shade's most recent check got no reply, the
  last reading is older than the stale window (three sweep rounds, at least
  100 s; what a dead serial port looks like), or the sweep has not reached the
  shade yet since Home Assistant started (with eight shades at 30 s, up to
  about four minutes after a restart). The hub's cached `rs` is never shown
  while the sweep is on, not even after the stale window: on a Pulse Pro it is
  of unknown age. A later reply restores the number. With the sweep off
  (the default, option at 0) nothing changes: the cached `rs` is the value.
  Position handling is untouched.

### Added

- **`Missed signal checks` sensor** per shade (`sensor.<shade>_missed_signal_checks`,
  diagnostic, `total_increasing`, no unit) with a `checks` attribute carrying
  the total number of checks, so the miss rate is `misses / checks`. A check is
  a sweep query whose serial session opened; a miss is a check that ended
  without the shade's reply. A serial port that cannot be reached at all counts
  neither, because that is the hub's fault, not the shade's. Only created
  while the sweep is on: with it off no check is ever made and the sensor
  would sit at 0 for good. (Turning the sweep off later leaves the entity in
  the registry as unavailable until it is removed by hand; a known wart.) Both
  counts start again at 0 on a restart, which `total_increasing` records as a
  meter reset.
- The options form's description of the live query interval says so.

### Testing

- 68 tests (60 before): a miss counted on silence, a check counted on a
  reply, nothing counted when the port is unreachable (refused, and connect
  timeout), the signal going `unknown` after a miss and back on the next
  reply (including a reply with the same dBm, which must still notify), no
  fallback to the cached `rs` with the sweep on, before the first reading and
  after the stale window alike, sweep-off behaviour unchanged, and the new
  sensor's state and `checks` attribute in Home Assistant, driven through the
  fake serial port.

## 1.1.0 (2026-10-08)

Audit release: the October 2026 security and correctness audit of the
integration (upstream `f9015a6`, v1.0.1) and of `aiopulse2` 1.0.0, which is now
vendored under `custom_components/automate/vendor/aiopulse2/` so the findings
that live in it could be fixed. `custom_components/automate/vendor/VENDORED.md`
records the upstream version and every change.

### Breaking changes

- **The signal sensor's unit is dBm, not dB**, and its value is dBm whichever
  path delivered it. Upstream stored the serial protocol's raw register (`R68`
  as 104) alongside the websocket's -82, so the same reading flipped between
  the two scales. Long-term statistics for an existing signal sensor will show
  the unit change once.
- **The signal sensor is enabled by default** (upstream shipped it disabled);
  both sensors are in the diagnostic category.
- **`aiopulse2` is no longer a requirement**; the manifest declares the vendored
  code's own dependency (`websockets`).
- **Entity naming uses `has_entity_name`**: the cover carries the shade's name,
  the sensors are "<shade> Battery" and "<shade> Signal". Entity ids and unique
  ids are unchanged for existing installs.

### Fixed

- **Signal scale flip** (library `set_signal`): serial `,R<hex>` is converted
  with dBm = R/2 - 134, so `R54` reads -92 dBm. Tests cover both paths and the
  round trip.
- **No entities when the hub is slow on port 1487.** The serial name lookup
  gave up after one 3 s silence (its retry loop was unreachable), left its
  socket open, died on a reply for an unknown id, and was never started again;
  with callbacks held until names are known, Home Assistant then created no
  entities until a restart. The lookup now retries with a back-off, falls back
  to the placeholder name `Shade <id>` after three attempts, and asks again
  hourly. Know that entity ids are minted from the first name: if the hub is
  slow at first setup, the entities are `cover.shade_abc`,
  `sensor.shade_abc_signal` for good (the friendly names update when the real
  name arrives). Set up the integration while the hub is responsive, or rename
  the entities afterwards.
- **A shadow without `shades` tore the websocket down** (`KeyError` in the
  consumer) and reconnected 10 s later, for every such message. Parsing is
  defensive throughout; one malformed shade no longer drops the whole message.
- **Missing keys read as "offline and closed".** Upstream defaulted `ol` to
  False and `mp` to 100, so a sparse or truncated update marked a shade
  unavailable and closed until the next poll. Only keys the hub sent are
  applied.
- **Truncated JSON (Pulse Pro).** When the cut falls inside a shade's own
  object, that shade is skipped for the poll instead of taking a possibly
  partial value. An unparseable message warns once per connection, then logs
  at debug (upstream: ERROR every 2 s).
- **Battery sensor never appeared** when the first update lacked `vo`: the
  platform recorded the shade as seen and the hub-level update that would have
  retried never fired. Learning the motor type is now a hub-level change, and a
  shade is only recorded once its entity was added.
- **The same hub could be added twice.** The config flow sets the hub id as the
  entry's unique id and aborts with `already_configured` (updating the host if
  the hub moved). The connection test is bounded at 20 s; connection errors are
  `cannot_connect`, anything else is logged as a bug and shown as `unknown`;
  the title falls back to `Pulse Hub (<host>)` for an unnamed hub.
- **Deprecated Home Assistant APIs** (HA 2026.9 logs a warning on each use;
  both stop working in 2027.8): the `via_device` identifier tuple (now
  `via_device_id`) and `device_registry.async_get_device` (now
  `async_get_device_by_identifier`). Cleanups alongside, not deprecations:
  sensors return `native_value` instead of overriding `state`, `should_poll`
  is a class attribute, the `CONN_CLASS_LOCAL_PUSH` constant (unused by HA
  since 2021.6) is gone, and the cover's tilt methods, which were misnamed and
  never enabled, are removed.
- **State writes off the event loop.** The entity callback was a plain
  function, which the library ran in an executor thread for every update; it
  is a coroutine now and writes state on the loop.
- **Commands could hang forever** waiting for the websocket; they fail after
  10 s with `NotConnectedException`.
- **Background tasks** (heartbeat, serial lookup, sweep) are tracked and
  cancelled on stop; the config flow's connection test no longer leaves a
  heartbeat and a serial task running; unload cancels the connection task
  instead of waiting out its reconnect sleep; an options change reloads the
  entry (and only an options change, not the hub's own title updates).
- **Hub device**: reports the model the hub announces ("Pulse Pro Hub" via the
  Matter vendor/product mapping), its firmware version and MAC, manufacturer
  "Rollease Acmeda", instead of a hard-coded "Pulse V2 Hub".
- Raw hub messages are no longer logged at INFO; Python 3.14 deprecations
  (`asyncio.iscoroutinefunction`, `get_event_loop` outside a loop) addressed;
  TLS 1.2 floor on the hub connection; `async_timeout` dependency dropped.

### Load on the hub

Upstream asked the hub for its full shadow every 2 s: 1,800 requests an hour,
43,200 a day, each one a serialisation of the whole state document (about 2.3
kB on the Pulse Pro logs in aiopulse2 #15, so roughly 100 MB a day of JSON from
one client) on a hub that also runs a Matter stack. **The default is now 10 s**
and the interval is an option (2 to 300 s): Settings, Devices & services,
Automate Pulse Hub v2, Configure. The cost: the cover entities learn about a
move made from the app or a remote up to one poll later, so 10 s instead of
2 s. 30 to 60 s is plenty when the shades are driven through Matter and this
integration only supplies battery and signal.

### Added: live signal sweep (opt-in, off by default)

On the Pulse Pro the websocket's `rs` is the hub's cached last signal and does
not refresh on its own, while the serial query `!<id>r?;` polls the motor live
over RF and returns a fresh reading every time (a few seconds each). The new
option "Live signal query interval" asks ONE shade per interval, round-robin
(at 30 s with eight shades each refreshes about every four minutes). While it
is on, a live reading (signal and position) takes precedence over the hub's
cached shadow values for a short window, three sweep rounds, then the cached
values apply again. The reason for including position is unverified field
observation on one hub that the cached position looked stale after moves made
from a remote; treat it as a hypothesis. A move or stop sent through Home
Assistant clears the position window at once, so the hub's reports for that
move are never held back. Set the option back to 0 and the cached values are
used again, no code change. The option accepts 0 (off) or 10 to 3600 s. One RF
exchange per interval is a small fraction of the hub's own traffic; the
integration never opens port 1487 twice at once, and a dead port cannot hold
the serial lock for more than 10 s.

### Known, left as is

- Unloading the entry unloads the platforms first and stops the library
  second (upstream's order, `__init__.py`); a hub update arriving in between
  is dispatched to platforms that are already gone. Harmless in practice (the
  listeners are disconnected by then), noted for completeness.

### Testing

- 60 tests under `test/`: the vendored library against fake sockets (a scripted
  websocket, a fake serial hub on 127.0.0.1) and the integration on Home
  Assistant 2026.9.1 (`pytest-homeassistant-custom-component` 0.13.364): config
  flow, options flow and reload, entities, devices and `via_device_id`,
  availability, unload. No test touches the network. Run:
  `python -m pytest -q`.
- `hacs.json` declares the minimum Home Assistant (2026.9.0) and drops the
  unused `iot_class` list; the manifest's `iot_class` is `local_polling`, which
  is what the integration does.
