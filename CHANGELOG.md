# Changelog

This is Lukas Pleva's fork of
[sillyfrog/Automate-Pulse-v2](https://github.com/sillyfrog/Automate-Pulse-v2).
It carries the October 2026 audit fixes upstream does not have; a Saturday job
merges upstream into it and the delta is audited before it reaches `main`.

## Unreleased

### Fixed

- **A closed shade no longer flashes "open" with a negative position.** The hub
  sometimes reports a position that cannot exist: 125, 155 and 200 for shades
  that were closed (100) and had not moved (`cover.master_bedroom_main`
  2026-11-05, `cover.living_room_center` 2026-11-03 and 2026-11-05). Both
  places a position arrives took any integer: the serial reply to the sweep's
  `!<id>r?;` query is three digits (000..999), and the hub shadow's `mp` is
  whatever number the hub wrote. The cover entity computes `100 - closed
  percent`, so 125 became position -25 and, because -25 is not "closed", state
  "open", until the next good reading flipped it back. The timing points to
  the serial path: each bad value lasted exactly one sweep round, the time
  until the sweep queried that shade again (1.5 min on the 2-shade hub, 4.5 min
  on the 8-shade hub), while the hub's shadow is polled every 30 s. A position
  outside 0..100 is now dropped by the library on both paths: the shade keeps
  its last good position and state, the reply's signal reading is still used,
  a dropped serial reading does not count as a fresh live reading, and one
  debug line records the raw value and the shade (`ignoring impossible serial
  position '125' for roller M4G ...`). Because production runs without debug
  logging, each shade also counts its rejected readings and keeps the last
  one (raw value, which path, how long ago), shown in the diagnostics dump
  under the shade's `position` section as `rejected_readings` and
  `last_rejected_reading`. Nothing is clamped to 0 or 100, since a garbled
  reading could be wrong in either direction.
- **Two hubs can both hold a motor with the same id.** A motor id ("M4G") is
  three characters the HUB assigns at pairing, so it is only unique per hub,
  and the house now has two: the living room's right shade on one hub and the
  spare bedroom's side shade on another are both "M4G" (found 2026-11-04 while
  adding the sixth hub). Every per-shade unique id and device identifier was
  the bare motor id, so Home Assistant would have dropped the second hub's
  "M4G" entities as duplicates and merged its device into the first hub's; the
  signal that removes a vanished shade's entities was keyed the same way and
  would have removed both hubs' copies. Every id is now `<hub id>_<motor id>`
  (the cover's unique id and the device identifier; the sensors and the button
  add their suffix as before: `2023369_M4G_signal`), the hub id being the
  entry's unique id as the config flow records it, or the entry id for an entry
  that has none. The hub's own device keeps its bare hub id, which can never
  equal a shade's id. The `refresh_signal` action, Delete on a stale device
  and the per-device diagnostics read the new form.
- **Existing installs are migrated in place; nothing visible changes.** The
  config entry is now version 2, and on the first load the integration
  rewrites the entity registry's unique ids and the shade devices' identifiers
  to the new form. Entity ids, names, icons, areas, labels, a disabled entity,
  history and dashboards all stay, because only those two registry fields
  move; the hub's device is not touched. The migration is safe to run again
  (an id that already carries the hub id is left alone), and an id whose new
  form is already taken (a registry half migrated and then run under an older
  release) is logged and skipped rather than failing the entry. **Rolling back
  to 1.5.x after this release needs the pre-update backup:** Home Assistant
  refuses to load a version-2 entry with the older code (by design, since the
  older code would mint the bare ids again and create a second copy of every
  shade).

### Testing

- 122 tests (109 before), in `test/test_hub_qualified_ids.py`: every entity's
  unique id and the shade device's identifier carry the hub id and the hub's
  own device keeps its bare id; a hub resolves only its own ids, and "M4" is
  never mistaken for "M4G"; an entry without a hub id is scoped by its entry
  id. Two hubs that both hold "M4G": both shades exist with their own entities,
  devices and positions, the `refresh_signal` action queries each hub's own
  M4G, the remove signal for one hub's M4G leaves the other's, and a stale
  device is judged against its own hub. The migration: a version-1 install
  with two shades and a stale device comes up with every entity id, device,
  area, user-given name, icon and disabled sensor kept and no "_2" copies; it
  runs once and twice without double-prefixing; a version-2 install is not
  written to; an entry without a hub id; a target id already taken is skipped
  with a warning; an entry from a newer release is refused.

## 1.5.2 (2026-11-01) and 1.5.1 (2026-10-24)

Two patch releases: the renamed-entry fix shipped in 1.5.1, the moved-hub fix
in 1.5.2.

### Fixed

- **A renamed hub entry stays renamed.** Every hub update wrote the entry's
  title as `<hub name> (<host>)`, so a name given to the entry in Home Assistant
  (Settings, Devices & services, the entry, Rename) was put back on the next
  update, and a reload or restart undid it for sure. With three hubs all called
  "My House" in the Pulse app, that left three entries told apart by address
  alone. The integration now writes the title only while it is still its own:
  the last title it wrote is recorded in the entry's data (`auto_title`), and
  the title is refreshed only while it still equals that record. An entry with
  no record yet counts as the integration's when its title is one the
  integration itself produces (the config flow's bare hub name or
  `Pulse Hub (<host>)`, or `<hub name> (<host>)`, `None (<host>)` included), so
  an entry set up by an earlier release is taken over once and a fresh entry
  still gets its `<hub name> (<host>)` title. A title that is still the
  integration's follows the hub when its name changes or its address changes.
  Anything else was typed by a person and is never touched. The title for a
  hub whose name has not arrived is `Pulse Hub (<host>)`, the same text as the
  hub's device name, instead of `None (<host>)`. A name given to the hub's
  device (the device page, the pencil) was already safe: the registry keeps
  `name_by_user` apart from the name the integration sets. The config flow's
  own title is unchanged.
- **A shade hub that moves to a new address is picked up without a restart.**
  Adding a hub that is already configured, at its new address, writes the new
  host into the existing entry. Home Assistant reloads the entry itself at
  that point today, but has said it will stop doing so in 2026.12 for an
  integration that has an update listener (this one has had one since the
  polling options; HA logs that deprecation today), and then the entry would
  keep talking to the old address until a restart. The integration's own
  listener now reloads the entry when the host changes, as HA asks, and the
  config flow no longer asks HA to reload as well (on today's HA, asking for
  both would be two reloads). A title or `auto_title` write by the hub still
  does not reload it, and a write that lands while the entry is unloading is
  ignored instead of setting the entry up again.

### Testing

- 109 tests (94 before): a running hub re-added at a new address is reloaded
  once, by the integration (HA's deprecation is not logged), and the fresh hub
  talks to the new address; the listener reloads on an options change and on
  a host change, and not on a title or `auto_title` write, nor on the same
  host written again, nor once the entry has unloaded. Then the title tests:
  a fresh entry gets the auto title from each of the
  four forms the integration produces, and the hub device's name matches it;
  an unnamed hub is titled by host until its name arrives; a renamed entry
  keeps its title across a hub update, an options-flow reload (the case seen
  live on 2026-10-24) and a restart with the hub renamed meanwhile, with the
  record left as the integration last wrote it; a title the integration never
  wrote is left alone with no record made; an auto title follows the hub's
  name, without reloading the entry, and follows the hub's address after a
  move; the hub device's `name_by_user` survives updates and a restart while
  the integration's own device name still follows the hub. Every push in that
  file names the shade and checks the update reached the device registry, since
  the library holds its callbacks while a shade is unnamed and a held update
  would pass a "nothing changed" check for the wrong reason.

## 1.5.0 (2026-10-21)

### Added

- **Delete a stale shade device.** Settings, Devices & services, the device,
  the three-dot menu, Delete now works for a shade this hub no longer reports:
  one unpaired in the Pulse app, or moved to another hub, whose device stayed
  behind. Before this, nothing ever removed a shade's device: the library never
  drops a shade from its list, so the integration's own removal path never ran.
  Delete is offered only for a device that has no entities and that this hub
  does not report. Home Assistant asks the integration first, and the answer
  is no for the hub's own device, for any shade the hub still lists (deleting
  that one would only have it recreated on the next update, with its entity
  customisations lost), and for any device that still has entity registry
  entries under this entry: right after a restart a truncated first shadow can
  leave a live shade out of the hub's list for a poll or two, and its cover and
  sensors are what tell it apart from a device left behind, which has none.
  No, too, while the entry's hub is not loaded or has not reported yet, since
  then there is no live list to check against. The check is against this
  entry's hub only: a device belongs to one config entry, so a motor that is
  live on another hub's entry does not keep its old device on this one.
  Read-only: it looks at the shade list and the registries already in memory
  and asks the hub for nothing.

### Testing

- 94 tests (87 before): a live shade and the hub device are rejected through
  Home Assistant's own device-removal command and stay registered; a stale
  device is deleted and the next hub update does not bring it back, with the
  live shade and the hub untouched; a shade missing from the hub's list that
  still has its entities is rejected and keeps them; refused before the hub's
  first report, after the entry is unloaded and with the library object gone,
  without raising; a device carrying none of this integration's identifiers is
  refused; two hubs, where the moved shades' stale copies on the first entry
  go while the live ones on the second entry, with the same ids, stay.

## 1.4.0 (2026-10-13)

### Added

- **Download diagnostics.** Settings, Devices & services, Automate Pulse Hub
  v2, the three-dot menu, Download diagnostics gives a JSON file of what the
  integration holds in memory; a shade's device page gives the same for that
  one shade. Read-only: nothing in it comes from a new request to the hub, so
  downloading it never touches the websocket or the serial port. It carries
  the hub (id, name, LAN address and MAC, model, firmware, the Matter ids, the
  hub's `onlineLatest`, connection state, the poll and sweep settings, the
  serial port's state and how many sessions opened it or could not, and the
  hub-level part of the last shadow as received) and every shade (id, name,
  its five entity ids, online, position with the live-reading rule in effect,
  signal as stored and as the sensor shows it with the live reading's age,
  the check and miss counters, battery voltage and percent, the `vo` string
  decoded into volts, motor type and motor firmware, and the shade's last
  shadow entry). The shadow sections are allow-listed: per shade only `mp`,
  `rs`, `ol`, `vo`, `ta`, `is` and `ls` pass through with their values, at
  hub level only `hubId`, `mac`, `name`, `firmware`, `mfi`, `matter` and
  `onlineLatest`; unknown fields are listed by name and type only, never their
  values (`"wifi": "<unknown field: dict, 2 keys>"`), so a field nobody has
  seen yet cannot leak. On top of that, sensitive keys are redacted wherever
  they appear inside the fields that did pass (Wi-Fi name and password,
  tokens and keys, latitude, longitude and timezone, serial numbers, account
  ids; the full list is `TO_REDACT` in `diagnostics.py`). The MAC and the LAN
  address are kept, since both stay on the LAN and both are needed to tell two
  hubs apart.
- In the library, each shade keeps a copy of its last parsed shadow entry and
  the hub keeps the hub-level fields (`Roller.last_shadow`,
  `Hub.last_shadow_hub`, each with a `_at` timestamp), and the hub counts
  serial sessions that opened the port and attempts that could not
  (`serial_sessions`, `serial_open_failures`). Kept in memory only, never
  logged. A shade cut off in a truncated message keeps its previous copy, and
  a message whose braces had to be restored is merged into the hub-level copy
  rather than replacing it, since its tail may have lost keys.

### Testing

- 87 tests (77 before): the diagnostics through Home Assistant's own
  endpoint for a hub with two shades (every section and the entity ids), the
  allowed shadow fields passing through and unknown ones showing as type and
  size only (a URL carrying a token among them), redaction of a token and a
  passcode inside the firmware and Matter objects with the MAC and host kept
  and no secret string anywhere in the JSON, no network call during the
  download (the library's serial, websocket and send paths all patched and
  never called), the per-device variants for a shade and for the hub, and a
  download before the hub has reported anything; in the library, the kept
  shadow per roller and for the hub, a truncated shade keeping its previous
  copy, a repaired message merging into the hub-level copy, and the serial
  session and open-failure counters against the fake port.

## 1.3.0 (2026-10-13)

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
