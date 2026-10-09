# Changelog

This is Lukas Pleva's fork of
[sillyfrog/Automate-Pulse-v2](https://github.com/sillyfrog/Automate-Pulse-v2).
It carries the October 2026 audit fixes upstream does not have; a Saturday job
merges upstream into it and the delta is audited before it reaches `main`.

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
  hourly.
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
- **Deprecated Home Assistant APIs** (HA 2026.9 warns on each; removed in
  2027.8): the `via_device` identifier tuple (now `via_device_id`),
  `device_registry.async_get_device` (now `async_get_device_by_identifier`),
  sensor `state` overrides (now `native_value`), the `should_poll` property,
  and the `CONN_CLASS_LOCAL_PUSH` constant (dead since 2021.6). The cover's
  tilt methods were misnamed, never enabled and are removed.
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
Automate Pulse Hub v2, Configure. 30 to 60 s is plenty when the shades are
driven through Matter and this integration only supplies battery and signal.

### Added: live signal sweep (opt-in, off by default)

On the Pulse Pro the websocket's `rs` is the hub's cached last signal and does
not refresh on its own, while the serial query `!<id>r?;` polls the motor live
over RF and returns a fresh reading every time (a few seconds each). The new
option "Live signal query interval" asks ONE shade per interval, round-robin
(at 30 s with eight shades each refreshes about every four minutes). While it
is on, the live reading is kept over the cached `rs`; set it back to 0 and the
cached value is used again, no code change. One RF exchange per interval is a
small fraction of the hub's own traffic; the integration never opens port 1487
twice at once.

### Testing

- 53 tests under `test/`: the vendored library against fake sockets (a scripted
  websocket, a fake serial hub on 127.0.0.1) and the integration on Home
  Assistant 2026.9.1 (`pytest-homeassistant-custom-component` 0.13.364): config
  flow, options flow and reload, entities, devices and `via_device_id`,
  availability, unload. No test touches the network. Run:
  `python -m pytest -q`.
- `hacs.json` declares the minimum Home Assistant (2026.9.0) and drops the
  unused `iot_class` list; the manifest's `iot_class` is `local_polling`, which
  is what the integration does.
