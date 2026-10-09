"""Acmeda Pulse Hub and Rollers Interfaces."""

# Note, these are in the same file to prevent circular imports
import asyncio
import functools
import inspect
import json
import logging
import ssl
import time
from typing import Any, Callable, Dict, List, Optional

import websockets.exceptions
from websockets.protocol import State

from . import const, errors
from .const import MovingAction

_LOGGER = logging.getLogger(__name__)

# The maximum voltage for the 8.3v battery, if the voltage is below this, the
# percentage calculation will use a different formula
BATTERY_8V_MAX_VOLTAGE = 9.0

# The minimum version above which we can trust the "ol" (online) value. Below this
# version, the roller will always be reported as online and the position reported will
# be optimistic (ie: not read from the roller)
# Disabling this behavior as it causes issues with other devices, however this should
# be OK, see https://github.com/sillyfrog/aiopulse2/issues/2
# Fixes https://github.com/sillyfrog/Automate-Pulse-v2/issues/15
ONLINE_MIN_VERSION = 0

# How often the hub is asked for its shadow (the full state of every shade), in
# seconds. Upstream hard-coded 2; the hub serialises the whole document for every
# request, so this is the single biggest load this library puts on it.
DEFAULT_POLL_INTERVAL = 10
MIN_POLL_INTERVAL = 2

# Websocket reconnect: first retry after RECONNECT_MIN seconds, doubling to
# RECONNECT_MAX. Reset on a successful connection.
RECONNECT_MIN = 5
RECONNECT_MAX = 60
# How long a command (move, stop) waits for the websocket to come back before it
# fails with NotConnectedException instead of hanging the caller.
SEND_HANDSHAKE_TIMEOUT = 10

# The serial (port 1487) name lookup: wait this long for a reply, retry with a
# doubling back-off, and after SERIAL_FALLBACK_ATTEMPTS failed attempts give each
# still-unnamed roller a placeholder name so callers are not blocked forever.
SERIAL_PORT = 1487
SERIAL_CONNECT_TIMEOUT = 10  # a dead port must not hold serial_lock for minutes
SERIAL_READ_TIMEOUT = 3
SERIAL_RETRY_MIN = 10
SERIAL_RETRY_MAX = 300
SERIAL_FALLBACK_ATTEMPTS = 3

# The websocket "rs" field is dBm. The serial protocol's ",R<hex>" field is a raw
# radio register; this is the conversion to dBm.
SERIAL_SIGNAL_OFFSET = 134

# Live signal sweep. On the Pulse Pro the websocket "rs" is the hub's CACHED last
# reading and does not refresh by itself; the serial "!<id>r?;" query polls the
# motor over RF and returns a fresh ",R<hex>" every time (a few seconds each).
# The sweep asks ONE shade every signal_poll_interval seconds, round-robin, so at
# 30 s with eight shades each one refreshes about every four minutes. OFF by
# default (0): a consumer opts in, and with it off the cached "rs" is used as is.
# With it ON the cached "rs" is never used: a shade's signal is its latest live
# reading, or None when its last check got no reply or the reading has gone stale
# (Roller.signal_reading). An old number shown as current is worse than no number.
DEFAULT_SIGNAL_POLL_INTERVAL = 0
MIN_SIGNAL_POLL_INTERVAL = 10
SIGNAL_POLL_REPLY_TIMEOUT = 8
# A live reading older than this many sweep rounds is stale: the signal reads None
# until the sweep recovers, and the websocket's cached "mp" applies to the
# position again.
SIGNAL_STALE_ROUNDS = 3

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
# The hub presents a self-signed certificate, so it cannot be verified against a CA.
# This context is used for exactly one connection: the websocket to the configured
# hub host (Hub.wsuri). It is never installed as a default and nothing else uses it.
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


def serial_signal_to_dbm(signal) -> Optional[float]:
    """Convert a serial ",R<hex>" signal register to dBm.

    Returns an int when the result is whole, so it compares equal to the websocket
    "rs" value for the same reading, or None when the input is not a hex byte.
    """
    try:
        raw = int(signal, 16) if isinstance(signal, str) else int(signal)
    except (TypeError, ValueError):
        return None
    dbm = raw / 2 - SERIAL_SIGNAL_OFFSET
    return int(dbm) if dbm.is_integer() else dbm


def next_backoff(current: float, minimum: float, maximum: float) -> float:
    """Return the delay to use after `current`: doubled, clamped to [minimum, maximum]."""
    if current <= 0:
        return minimum
    return min(max(current * 2, minimum), maximum)


def _repair_truncated_json(msg: str):
    """Parse a hub message, repairing the Pulse Pro's truncation if needed.

    Returns (parsed, truncated_shade_suspect) or (None, False) when the message
    cannot be parsed. Pulse Pro hubs (firmware 1.1.0 onwards) drop the final
    closing brace(s) of every shadow response; appending them restores the document.
    When the cut falls INSIDE a shade's own object (the shades container and at
    least one shade are both unterminated) the last shade's values may be partial,
    and the second return value is True so the caller can skip that shade.
    """
    try:
        return json.loads(msg), False
    except json.JSONDecodeError as e:
        _LOGGER.debug("Invalid JSON received: %s", e)

    missing_braces = msg.count("{") - msg.count("}")
    if missing_braces <= 0:
        return None, False
    try:
        parsed = json.loads(msg + "}" * missing_braces)
    except json.JSONDecodeError:
        return None, False
    _LOGGER.debug("Repaired truncated JSON by adding %d closing brace(s)", missing_braces)

    suspect = False
    shades_at = msg.rfind('"shades"')
    if shades_at >= 0:
        tail = msg[shades_at:]
        open_depth = tail.count("{") - tail.count("}")
        # depth 1: only the shades container is unterminated, every shade is whole.
        # depth 2+: a shade's own object was cut, so its last value may be partial.
        suspect = open_depth >= 2
    return parsed, suspect


class Hub:
    """Representation of an Acmeda Pulse v2 Hub."""

    def __init__(
        self,
        host: str,
        delay_callbacks: bool = True,
        propagate_callbacks: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        serial_port: int = SERIAL_PORT,
        signal_poll_interval: float = DEFAULT_SIGNAL_POLL_INTERVAL,
    ):
        """Init the hub.

        host: The IP address / hostname of hub
        delay_callbacks: If True (default), no callbacks will be called until the
            inital hub sync is complete, getting details such as the device name
        propagate_callbacks: If True, when there is a change to the hub, all roller
            callbacks are also notified.
        poll_interval: seconds between shadow requests to the hub (minimum 2).
        serial_port: the hub's serial-protocol TCP port (1487 on real hubs; a
            parameter so tests can point it at a local fake).
        signal_poll_interval: seconds between live serial signal queries, one
            shade per query round-robin (minimum 10; 0 turns the sweep off).
        """
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
        self.handshake = asyncio.Event()
        self.delay_callbacks = delay_callbacks
        self.propagate_callbacks = propagate_callbacks
        self.response_task = None
        self.running = False
        self.connected = False
        self.lasterrorlog = None
        self.serialok = False
        self.lastserialerror = None

        self.name = None
        self.id = None
        self.host = host
        self.wsuri = "wss://{}:443/rpc".format(self.host)
        self.serial_port = serial_port
        self.mac_address = None
        self.firmware_ver = None
        self.model = None

        self.ws = None

        self.rollers = {}
        self.unknown_rollers = set()
        self.rollers_known = asyncio.Event()
        self.rollers_known.clear()
        self.serialrunning = False
        self.serial_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.signal_task: Optional[asyncio.Task] = None
        # One serial-port session at a time: the name lookup and the signal sweep
        # share the hub's single port 1487.
        self.serial_lock = asyncio.Lock()
        self._sweep_position = 0
        self.sent_details_request = set()
        self.payload_queue = []
        self.json_failures = 0

        self.handshake.clear()
        self.update_callbacks: List[Callable] = []
        self.heartbeatinterval = max(float(poll_interval), MIN_POLL_INTERVAL)
        if signal_poll_interval and signal_poll_interval > 0:
            self.signal_poll_interval = max(
                float(signal_poll_interval), MIN_SIGNAL_POLL_INTERVAL
            )
        else:
            self.signal_poll_interval = 0.0

    def __str__(self):
        """Returns string representation of the hub."""
        return (
            f"Name: {self.name!r} "
            f"ID: {self.id} "
            f"Host: {self.host} "
            f"MAC: {self.mac_address} "
            f"Firmware Version: {self.firmware_ver} "
            f"Model: {self.model} "
        )

    def callback_subscribe(self, callback: Callable):
        """Add a callback for hub updates."""
        if callback not in self.update_callbacks:
            self.update_callbacks.append(callback)

    def callback_unsubscribe(self, callback: Callable):
        """Remove a callback for hub updates."""
        if callback in self.update_callbacks:
            self.update_callbacks.remove(callback)

    def async_add_job(
        self, target: Callable[..., Any], *args: Any
    ) -> Optional[asyncio.Future]:
        """Add a job from within the event loop.

        This method must be run in the event loop.

        target: target to call.
        args: parameters for method to call.
        """
        task = None

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        if asyncio.iscoroutine(check_target):
            task = self.loop.create_task(target)  # type: ignore
        elif inspect.iscoroutinefunction(check_target):
            task = self.loop.create_task(target(*args))
        else:
            task = self.loop.run_in_executor(None, target, *args)  # type: ignore

        return task

    def unnamed_rollers(self) -> List[str]:
        """Ids of rollers that have no name at all yet (not even a placeholder)."""
        return [rid for rid, roller in self.rollers.items() if roller.name is None]

    def notify_callback(self):
        """Tell callback that the hub has been updated."""
        if self.delay_callbacks and (self.unnamed_rollers() or len(self.rollers) == 0):
            return
        for callback in self.update_callbacks:
            self.async_add_job(callback, self)
        if self.propagate_callbacks:
            for roller in self.rollers.values():
                roller.notify_callback()

    async def disconnect(self):
        """Disconnect from the hub."""
        _LOGGER.debug("%s: Disconnecting", self.host)
        if self.ws:
            await self.ws.close()
        self.handshake.clear()
        _LOGGER.info("%s: Disconnected", self.host)

    def response_parse(self, response: str):
        """Decode response."""
        response = response.strip()
        for name, matcher in const.ALL_RESPONSES.items():
            match = matcher.match(response)
            if match:
                _LOGGER.debug(
                    "%s: Received response: %s content: %s",
                    self.host,
                    name,
                    match.groups(),
                )
                handler = getattr(self, "handle_" + name.lower(), "")
                if handler:
                    handler(**match.groupdict())
                else:
                    _LOGGER.debug("No handler for %s", name)
                return
        _LOGGER.debug("No match for: %s", response)

    def handle_device_query_position_response(
        self, id: str, closedpercent: str, tiltpercent: str, signal: str
    ):
        roller = self.rollers.get(id)
        if roller is None:
            _LOGGER.debug("Serial position response for unknown roller %s", id)
            return
        # Live values from the motor: position and signal both follow the
        # "live wins until stale" rule (use_ws_signal, use_ws_position), so the cached
        # shadow cannot flap them back between sweep replies.
        changed = self.applychanges(
            roller,
            {
                "closed_percent": forcetoint(closedpercent),
                "tilt_percent": forcetoint(tiltpercent),
            },
        )
        changed = roller.set_signal(signal) or changed
        if roller.signal_missed or roller.signal_stale:
            # A reply after a miss or a stale spell restores the reading even if
            # the dBm is the same number as before, so consumers must be told.
            roller.signal_missed = False
            roller.signal_stale = False
            changed = True
        now = time.monotonic()
        roller.serial_signal_at = now
        roller.serial_position_at = now
        if changed:
            roller.notify_callback()

    def handle_device_query_name_response(self, id: str, name: str):
        roller = self.rollers.get(id)
        if roller is None:
            _LOGGER.debug("Serial name response for unknown roller %s", id)
            return
        roller.name = name
        roller.name_is_placeholder = False
        self.unknown_rollers.discard(id)
        roller.notify_callback()
        if not self.unknown_rollers:
            # The list of unknown_rollers is empty
            self.rollers_known.set()
            self.notify_callback()

    async def _serial_session(self, commands, read_timeout, done=None) -> None:
        """One serial-port session: send `commands`, parse replies until quiet.

        Stops early when `done()` returns True. Holds serial_lock so the name
        lookup and the signal sweep never open port 1487 at the same time.
        """
        async with self.serial_lock:
            async with asyncio.timeout(SERIAL_CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    self.host, port=self.serial_port
                )
            try:
                for buf in commands:
                    writer.write(buf.encode())
                await writer.drain()
                while True:
                    try:
                        async with asyncio.timeout(read_timeout):
                            response = await reader.readuntil(b";")
                    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                        # Quiet for read_timeout, or the hub closed: session over
                        break
                    _LOGGER.debug("recv < %s", response)
                    self.response_parse(response.decode(errors="replace"))
                    if done is not None and done():
                        break
            finally:
                writer.close()

    async def _serial_query_names(self, rollerids) -> None:
        """Ask the serial port for each roller's name, read until quiet."""
        await self._serial_session(
            [const.DEVICE_QUERY_NAME.format(id=rollerid) for rollerid in rollerids],
            SERIAL_READ_TIMEOUT,
        )

    async def _serial_query_position(self, rollerid: str) -> None:
        """Ask the motor, live over RF, for its position and signal.

        Counts the outcome on the roller. A check is a session that opened; a
        miss is a check that ended (SIGNAL_POLL_REPLY_TIMEOUT of quiet, or the
        hub closing) without this roller's reply. A port that cannot be reached
        at all raises out of _serial_session before anything is counted: that is
        the hub's problem, not the shade's.
        """
        roller = self.rollers[rollerid]
        before = roller.serial_signal_at
        await self._serial_session(
            [const.DEVICE_QUERY_POSITION.format(id=rollerid)],
            SIGNAL_POLL_REPLY_TIMEOUT,
            done=lambda: roller.serial_signal_at != before,
        )
        roller.signal_checks += 1
        if roller.serial_signal_at == before:
            roller.signal_misses += 1
            roller.signal_missed = True
            _LOGGER.debug(
                "%s: no signal reply from %s (%d of %d checks missed)",
                self.host,
                rollerid,
                roller.signal_misses,
                roller.signal_checks,
            )
        # The counts changed either way (a reply already notified for the
        # reading itself; this is for the counters).
        roller.notify_callback()

    def signal_stale_after(self) -> float:
        """Seconds after which a live serial reading no longer counts as current."""
        rounds = max(len(self.rollers), 1) * self.signal_poll_interval
        return max(SIGNAL_STALE_ROUNDS * rounds, 10 * MIN_SIGNAL_POLL_INTERVAL)

    def _live_reading_is_fresh(self, read_at) -> bool:
        """True while a live serial reading taken at `read_at` should win."""
        if self.signal_poll_interval <= 0 or read_at is None:
            return False
        return time.monotonic() - read_at <= self.signal_stale_after()

    def use_ws_signal(self, roller) -> bool:
        """True when the websocket's cached "rs" applies to this roller.

        Only while the sweep is off. With it on, the cached value is never
        applied, not even before the first live reading or after the live
        reading has gone stale: on a Pulse Pro "rs" is of unknown age, and
        Roller.signal_reading reports None for those cases instead.
        """
        return self.signal_poll_interval <= 0

    def use_ws_position(self, roller) -> bool:
        """True when the websocket's cached "mp" applies to this roller.

        Same rule as the signal, with its own timestamp: a command sent through
        this library (move_to, move_stop) clears it, so the hub's position
        reports for that move are applied at once instead of being held back
        until the sweep next reads the shade.
        """
        return not self._live_reading_is_fresh(roller.serial_position_at)

    def expire_stale_signal_readings(self) -> None:
        """Flag, once, every live reading that has aged past signal_stale_after().

        Consumers only hear from a roller when it notifies, and a hub poll only
        notifies on a hub-level change, so without this a reading would keep
        showing after the serial port went down. Called every sweep tick (so
        while the port is down, at least every SERIAL_RETRY_MAX seconds); the
        next reply clears the flag.
        """
        for roller in self.rollers.values():
            if (
                roller.serial_signal_at is not None
                and not roller.signal_stale
                and not self._live_reading_is_fresh(roller.serial_signal_at)
            ):
                roller.signal_stale = True
                _LOGGER.debug("%s: live signal reading for %s is stale", self.host, roller.id)
                roller.notify_callback()

    async def signal_sweep(self):
        """Query one shade's live signal every signal_poll_interval seconds."""
        backoff = 0.0
        while self.running and self.signal_poll_interval > 0:
            await asyncio.sleep(max(backoff, self.signal_poll_interval))
            if not self.running:
                break
            self.expire_stale_signal_readings()
            named = [rid for rid, r in self.rollers.items() if r.name is not None]
            if not named or self.unknown_rollers:
                # Nothing to sweep yet, or the name lookup needs the port first
                continue
            self._sweep_position %= len(named)
            rollerid = named[self._sweep_position]
            self._sweep_position += 1
            try:
                await self._serial_query_position(rollerid)
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError) as e:
                if self.lastserialerror != str(e):
                    _LOGGER.warning(
                        "%s: serial port %d unavailable for the signal sweep: %s",
                        self.host,
                        self.serial_port,
                        e,
                    )
                    self.lastserialerror = str(e)
                backoff = next_backoff(backoff, SERIAL_RETRY_MIN, SERIAL_RETRY_MAX)
            except Exception as e:  # keep sweeping; one bad reply is not fatal
                _LOGGER.debug("%s: signal sweep error on %s: %s", self.host, rollerid, e)

    def _apply_fallback_names(self) -> None:
        """Name every still-unknown roller after its id so callers can proceed.

        The real name replaces it when the serial port answers later
        (handle_device_query_name_response clears the placeholder flag).
        """
        for rollerid in list(self.unknown_rollers):
            roller = self.rollers.get(rollerid)
            if roller is not None and roller.name is None:
                roller.name = f"Shade {rollerid}"
                roller.name_is_placeholder = True
                _LOGGER.warning(
                    "%s: no name from the hub's serial port for roller %s after %d "
                    "attempts; using %r until it answers",
                    self.host,
                    rollerid,
                    SERIAL_FALLBACK_ATTEMPTS,
                    roller.name,
                )
                roller.notify_callback()
        self.unknown_rollers.clear()
        self.rollers_known.set()
        self.notify_callback()

    async def serialrunner(self):
        """The running to get all required information from the hub

        This will exit when complete.
        """
        self.serialrunning = True
        attempts = 0
        delay = 0.0
        try:
            while self.unknown_rollers and self.running:
                self.rollers_known.clear()  # We have some unknown rollers
                try:
                    await self._serial_query_names(list(self.unknown_rollers))
                except (OSError, asyncio.TimeoutError) as e:
                    # Connection refused / no route / connect or handshake timeout
                    if self.lastserialerror != str(e):
                        _LOGGER.warning(
                            "%s: serial port %d unavailable: %s",
                            self.host,
                            self.serial_port,
                            e,
                        )
                        self.lastserialerror = str(e)
                except Exception as e:  # a bad reply must not end the lookup
                    _LOGGER.info("Error in serial running: %s", e)
                if not self.unknown_rollers:
                    self.serialok = True
                    self.lastserialerror = None
                    break
                attempts += 1
                if attempts >= SERIAL_FALLBACK_ATTEMPTS:
                    # Names the port never gave: placeholders for rollers that
                    # have none, and release the queue either way (a roller
                    # re-queued from a placeholder keeps it). The hourly tick
                    # asks again.
                    self._apply_fallback_names()
                    break
                delay = next_backoff(delay, SERIAL_RETRY_MIN, SERIAL_RETRY_MAX)
                _LOGGER.debug(
                    "%s: %d roller(s) still unnamed, retrying serial in %ss",
                    self.host,
                    len(self.unknown_rollers),
                    delay,
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            self.serialrunning = False

    async def runserial(self):
        """Runs a 'serial' connection to the hub to get additional information"""
        if self.serialrunning:
            # Already running, don't start again
            return
        self.serialrunning = True
        self.serial_task = asyncio.create_task(self.serialrunner())

    def requeue_placeholder_names(self) -> None:
        """Ask the serial port again for rollers with a placeholder name or none."""
        for rollerid, roller in self.rollers.items():
            if roller.name_is_placeholder or roller.name is None:
                self.unknown_rollers.add(rollerid)
        if self.unknown_rollers and self.running and not self.serialrunning:
            self.serialrunning = True
            self.serial_task = asyncio.create_task(self.serialrunner())

    async def send_payload(self, jscommand: Dict):
        """Send payload to the hub

        See sendws for details.
        """
        if not self.running:
            raise errors.NotRunningException
        try:
            async with asyncio.timeout(SEND_HANDSHAKE_TIMEOUT):
                await self.handshake.wait()
        except asyncio.TimeoutError:
            raise errors.NotConnectedException from None
        _LOGGER.debug("Sending payload: %s", jscommand)
        if not await self.sendws(jscommand):
            raise errors.NotConnectedException

    async def sendws(self, jscommand: Dict) -> bool:
        """Send jscommand over the websocket

        jscommand must be a Python dict, that will be converted to JSON
        Returns True on success
        """
        if self.ws:
            try:
                async with asyncio.timeout(10):
                    await self.ws.send(json.dumps(jscommand))
                return True
            except (
                websockets.exceptions.WebSocketException,
                asyncio.TimeoutError,
            ) as e:
                _LOGGER.warning("Error sending payload: %s", e)
                self.handshake.clear()
        return False

    async def heartbeat(self):
        """Poll the hub's shadow every heartbeatinterval seconds.

        The hub does not push state over the websocket, so this is how every
        position, battery and signal reading arrives. Queued payloads (the
        once-an-hour details query) go out one per tick ahead of the poll.
        """
        reset_request_counter = 3600 / self.heartbeatinterval
        while self.running:
            try:
                if self.ws and self.ws.state == State.OPEN and self.handshake.is_set():
                    if self.payload_queue:
                        payload = self.payload_queue.pop(0)
                        success = await self.sendws(payload)
                        if not success:
                            self.payload_queue.append(payload)
                    await self.sendws(
                        {"method": "shadow", "src": "app", "id": int(time.time())}
                    )
                reset_request_counter -= 1
                if reset_request_counter <= 0:
                    # About once an hour allow a request to be made to the hub to
                    # get the rollers details again, and retry any names the
                    # serial port never supplied.
                    self.sent_details_request.clear()
                    self.requeue_placeholder_names()
                    reset_request_counter = 3600 / self.heartbeatinterval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep polling; this loop is the hub's pulse
                _LOGGER.warning("%s: heartbeat error, polling continues: %s", self.host, e)
            await asyncio.sleep(self.heartbeatinterval)

    def applychanges(self, obj: Any, newvalues: Dict[str, Any]) -> bool:
        """Applies and reports changes from newvals to the attributes of obj

        Returns True if there were any changes, False otherwise.
        """
        updated = False
        for attr, val in newvalues.items():
            if getattr(obj, attr) != val:
                setattr(obj, attr, val)
                updated = True
        return updated

    def _roller_newvals(self, rollerid: str, roller: Dict[str, Any]) -> Dict[str, Any]:
        """Translate one shade's shadow entry into Roller attribute values.

        Only keys the hub actually sent are translated: a missing key means "no
        news", never "offline" or "closed". A truncated Pulse Pro message can drop
        keys from the last shade, and defaults here would make that shade flap.
        """
        newvals: Dict[str, Any] = {}
        current = self.rollers[rollerid]
        if "rs" in roller and self.use_ws_signal(current):
            newvals["signal"] = roller["rs"]
        if "is" in roller:
            newvals["moving"] = not roller["is"]
        if "ol" in roller:
            newvals["online"] = bool(roller["ol"])
        if roller.get("mp") is not None and self.use_ws_position(current):
            try:
                newvals["closed_percent"] = int(roller["mp"])
            except (TypeError, ValueError):
                _LOGGER.debug("Roller %s: unusable position %r", rollerid, roller["mp"])
        if "vo" not in roller:
            # The voltage and version key is missing, request more details if
            # not already sent. If this request is not made, it will typically
            # be sent through with in about 20 minutes
            if rollerid not in self.sent_details_request:
                self.payload_queue.append(
                    {
                        "method": "shadow",
                        "args": {
                            "desired": {"shades": {rollerid: {"query": True}}},
                            "timeStamp": time.time(),
                        },
                    }
                )
                self.sent_details_request.add(rollerid)
        else:
            batteryinfo = const.WS_ROLLER_VOLTAGE.match(str(roller["vo"]))
            if batteryinfo:
                newvals["battery"] = float(batteryinfo.group("voltage"))
                newvals["devicetypeshort"] = batteryinfo.group("type")
                newvals["devicetype"] = const.TYPES.get(
                    batteryinfo.group("type"),
                    f"unknown {batteryinfo.group('type')}",
                )
                newvals["version"] = batteryinfo.group("version")

        try:
            version = int(
                newvals.get("version") or forcetoint(self.rollers[rollerid].version)
            )
            if version < ONLINE_MIN_VERSION:
                newvals["online"] = True
                newvals["closed_percent"] = 50
        except Exception:
            pass
        return newvals

    async def wsconsumer(self, msg: str):
        jsmsg, truncated_shade_suspect = _repair_truncated_json(msg)
        if jsmsg is None:
            self.json_failures += 1
            if self.json_failures == 1:
                _LOGGER.warning(
                    "%s: could not parse a hub message even after repairing "
                    "truncation; further occurrences are logged at debug level",
                    self.host,
                )
            else:
                _LOGGER.debug("%s: unparseable hub message dropped", self.host)
            return

        if not isinstance(jsmsg, dict) or not isinstance(jsmsg.get("result"), dict):
            _LOGGER.debug("Got unknown WS response: %s", msg)
            return
        data = jsmsg["result"].get("reported")
        if not isinstance(data, dict):
            _LOGGER.debug("Got unknown WS response: %s", msg)
            return
        if not self.connected:
            self.connected = True
            self.notify_callback()
        if self.lasterrorlog is not None:
            _LOGGER.info("Connected to %s", self.host)
            self.lasterrorlog = None
        _LOGGER.debug("Got payload: %s", data)
        # Determine model from mfi field or Matter vendor/product ID mapping
        # workaround for Pulse Pro Hub
        mfi = data.get("mfi")
        model = mfi.get("model") if isinstance(mfi, dict) else None
        matter_info = data.get("matter")
        if not model and isinstance(matter_info, dict):
            vendor_id = matter_info.get("vendorID")
            product_id = matter_info.get("productID")
            if vendor_id and product_id:
                model = const.MATTER_MODEL_MAPPING.get((vendor_id, product_id))
        firmware = data.get("firmware")

        newvals = {
            "name": data.get("name"),
            "id": data.get("hubId"),
            "mac_address": data.get("mac"),
            "firmware_ver": firmware.get("version") if isinstance(firmware, dict) else None,
            "model": model,
        }
        hubchanges = self.applychanges(self, newvals)

        shades = data.get("shades")
        if not isinstance(shades, dict):
            _LOGGER.debug("%s: shadow without a shades object", self.host)
            if hubchanges:
                self.notify_callback()
            return

        shade_items = list(shades.items())
        if truncated_shade_suspect and shade_items:
            skipped_id, _ = shade_items.pop()
            _LOGGER.debug(
                "%s: shade %s was cut off in a truncated message, skipped this poll",
                self.host,
                skipped_id,
            )

        for rollerid, roller in shade_items:
            if not isinstance(roller, dict):
                continue
            if rollerid not in self.rollers:
                self.rollers[rollerid] = Roller(self, rollerid)
                self.unknown_rollers.add(rollerid)
                await self.runserial()
                hubchanges = True

            try:
                newvals = self._roller_newvals(rollerid, roller)
            except Exception as e:  # one bad shade must not drop the whole message
                _LOGGER.debug("Roller %s: could not read shadow entry: %s", rollerid, e)
                continue

            current = self.rollers[rollerid]
            if "devicetypeshort" in newvals and current.devicetypeshort is None:
                # First time we learn what kind of motor this is (battery or not):
                # a hub-level change, so consumers can create the entities that
                # depend on it.
                hubchanges = True
            if self.applychanges(current, newvals):
                current.notify_callback()

        if hubchanges:
            self.notify_callback()

    async def run(self):
        """Start hub by connecting then awaiting for messages.

        Runs until the stop() method is called.
        """
        if self.running:
            _LOGGER.warning("%s: Already running", self.host)
            return
        self.running = True

        self.heartbeat_task = asyncio.create_task(self.heartbeat())
        if self.signal_poll_interval > 0:
            self.signal_task = asyncio.create_task(self.signal_sweep())
        retry_delay = 0.0
        try:
            while self.running:
                try:
                    async with websockets.connect(self.wsuri, ssl=ssl_context) as websocket:
                        self.ws = websocket
                        self.handshake.set()
                        self.json_failures = 0
                        retry_delay = 0.0
                        async for message in websocket:
                            await self.wsconsumer(message)
                except Exception as e:
                    self.ws = None
                    if self.running and self.lasterrorlog != errors.CannotConnectException:
                        _LOGGER.warning("Websocket Connection closed: %s", e)
                        self.lasterrorlog = errors.CannotConnectException
                    self.connected = False
                    self.handshake.clear()
                    self.notify_callback()
                    if self.running:
                        retry_delay = next_backoff(retry_delay, RECONNECT_MIN, RECONNECT_MAX)
                        _LOGGER.debug("%s: reconnecting in %ss", self.host, retry_delay)
                        await asyncio.sleep(retry_delay)
                else:
                    # Clean close by the hub: reconnect promptly
                    self.ws = None
                    self.connected = False
                    self.handshake.clear()
                    self.notify_callback()
                    if self.running:
                        retry_delay = next_backoff(retry_delay, RECONNECT_MIN, RECONNECT_MAX)
                        await asyncio.sleep(retry_delay)
        finally:
            await self._cancel_background_tasks()

        _LOGGER.debug("%s: Stopped", self.host)

    async def _cancel_background_tasks(self):
        """Cancel the heartbeat and serial tasks and wait for them."""
        tasks = [
            t
            for t in (self.heartbeat_task, self.serial_task, self.signal_task)
            if t and not t.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.heartbeat_task = None
        self.serial_task = None
        self.signal_task = None
        self.serialrunning = False

    async def test(self, update_devices=False):
        """Connect to the hub once, and check we get a valid response

        update_devices: if True, will wait until the initial full update is complete
        and the rollers have had their initial information populated.
        Will raise an exception if unable to connect and get a response, or True
        if connection succeeded.
        """
        self.running = True
        try:
            async with websockets.connect(self.wsuri, ssl=ssl_context) as websocket:
                self.ws = websocket
                self.heartbeat_task = asyncio.create_task(self.heartbeat())
                self.handshake.set()
                async for message in websocket:
                    await self.wsconsumer(message)
                    if self.connected:
                        break
                    else:
                        raise errors.InvalidResponseException
                else:
                    raise errors.InvalidResponseException
            # Now connected, wait for the initial device listing to be populated
            if update_devices:
                await self.rollers_known.wait()
        finally:
            self.running = False
            self.ws = None
            self.connected = False
            self.handshake.clear()
            await self._cancel_background_tasks()
        return True

    async def stop(self):
        """Tell hub to stop and await for it to disconnect."""
        if not self.running:
            _LOGGER.warning("%s: Already stopped", self.host)
            return
        _LOGGER.debug("%s: Stopping", self.host)
        self.running = False
        await self.disconnect()


class Roller:
    """Representation of a Roller blind."""

    def __init__(self, hub: Hub, roller_id: str):
        """Init a new roller blind."""
        self.hub = hub
        self.id = roller_id
        self.name = None
        self.name_is_placeholder = False
        self.devicetypeshort = None
        self.devicetype = None
        self.battery = None
        self.target_closed_percent = None
        self.closed_percent = None
        self.tilt_percent = None
        self.signal = None
        # time.monotonic() of the last LIVE reading from the serial port, None
        # until the sweep has queried this roller. Signal and position are kept
        # apart: a command through this library clears the position one.
        self.serial_signal_at = None
        self.serial_position_at = None
        # The live sweep's record for this roller: how many checks opened a
        # serial session, how many of those ended without its reply, and whether
        # the most recent one did. Counted in Hub._serial_query_position; reset
        # when the Hub is rebuilt (a restart), never otherwise. signal_stale is
        # set by Hub.expire_stale_signal_readings when the last live reading has
        # aged past signal_stale_after(); a reply clears both flags.
        self.signal_checks = 0
        self.signal_misses = 0
        self.signal_missed = False
        self.signal_stale = False
        self.version = None
        self._moving = False
        self.action = MovingAction.stopped
        self.online = False
        self.update_callbacks: List[Callable] = []

    def __str__(self):
        """Returns string representation of roller."""
        if self.action == MovingAction.down:
            actiontxt = "down"
        elif self.action == MovingAction.up:
            actiontxt = "up"
        else:
            actiontxt = "stopped"
        return (
            "Name: {!r} ID: {} Type: {} Target %: {} Closed %: {} Tilt %: {} Signal RSSI: {} Battery: {}v Battery %: {} Action: {}"
        ).format(
            self.name,
            self.id,
            self.devicetype,
            self.target_closed_percent,
            self.closed_percent,
            self.tilt_percent,
            self.signal,
            self.battery,
            self.battery_percent,
            actiontxt,
        )

    @property
    def battery_percent(self):
        """A rough approximation base on the app vs voltage levels read.

        Returns None if they devicetype is not D or U (DC motor), as there is no battery.

        Should be updated if a better solution is found.
        """
        if not self.has_battery or not self.battery:
            return None
        if self.battery < BATTERY_8V_MAX_VOLTAGE:
            # Smaller 8.3v battery
            percent = int(42.8 * self.battery - 255)
        else:
            percent = int(27.4 * self.battery - 255)
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100
        return percent

    @property
    def has_battery(self):
        """True if device appears to be battery operated"""
        return self.devicetypeshort in ("D", "U", "d")

    @property
    def signal_reading(self) -> Optional[float]:
        """The signal, in dBm, that can honestly be called current.

        Sweep off: the hub's cached "rs", as upstream reports it. Sweep on: the
        latest live reading, or None when there is no current one: the last
        check got no reply, the reading is older than signal_stale_after() (the
        serial port has been down), or the sweep has not reached this roller yet
        since the hub was built. The cached "rs" is never substituted: on a Pulse
        Pro it is of unknown age, which is why the sweep exists.
        """
        if self.hub.signal_poll_interval <= 0:
            return self.signal
        if self.signal_missed or self.signal_stale:
            return None
        if not self.hub._live_reading_is_fresh(self.serial_signal_at):
            return None  # between sweep ticks the clock is the truth, the flag follows
        return self.signal

    @property
    def moving(self):
        return self._moving

    @moving.setter
    def moving(self, new_val):
        self._moving = new_val
        if self._moving:
            if self.action == MovingAction.stopped:
                # Guess as to the direction
                if forcetoint(self.closed_percent) > 50:
                    self.action = MovingAction.up
                else:
                    self.action = MovingAction.down
        else:
            if self.action != MovingAction.stopped:
                self.action = MovingAction.stopped
            self.target_closed_percent = self.closed_percent

    def callback_subscribe(self, callback: Callable):
        """Add a callback for hub updates."""
        if callback not in self.update_callbacks:
            self.update_callbacks.append(callback)

    def callback_unsubscribe(self, callback: Callable):
        """Remove a callback for hub updates."""
        if callback in self.update_callbacks:
            self.update_callbacks.remove(callback)

    def notify_callback(self):
        """Tell callback that device has been updated."""
        if self.hub.delay_callbacks and self.name is None:
            return
        for callback in self.update_callbacks:
            self.hub.async_add_job(callback, self)

    def set_signal(self, signal) -> bool:
        """Set the signal, in dBm, from a serial ",R<hex>" register value.

        The websocket reports dBm directly ("rs"); the serial protocol reports a
        raw register that must be converted, otherwise the same reading flips
        between -82 and 104 depending on which path delivered it last.
        Returns True when the stored value changed.
        """
        dbm = serial_signal_to_dbm(signal)
        if dbm is None:
            _LOGGER.debug("Roller %s: unusable serial signal %r", self.id, signal)
            return False
        if self.signal == dbm:
            return False
        self.signal = dbm
        return True

    async def move_to(self, percent: int):
        """Send command to move the roller to a percentage closed."""
        if forcetoint(self.version) < ONLINE_MIN_VERSION:
            self.closed_percent = percent
        else:
            currentpcg = self.closed_percent
            if currentpcg is None:
                currentpcg = 50
            if percent > currentpcg:
                self.action = MovingAction.down
            elif percent < currentpcg:
                self.action = MovingAction.up
            else:
                self.action = MovingAction.stopped
            self._moving = True
            self.target_closed_percent = percent
        # We are moving the shade: the hub's position reports for this move must
        # apply at once, not wait out a live reading's freshness window.
        self.serial_position_at = None
        self.notify_callback()
        await self.hub.send_payload(
            {
                "method": "shadow",
                "args": {
                    "desired": {"shades": {self.id: {"movePercent": int(percent)}}},
                    "timeStamp": time.time(),
                },
            }
        )

    async def move_up(self):
        """Send command to move the roller to fully open."""
        await self.move_to(0)

    async def move_down(self):
        """Send command to move the roller to fully closed."""
        await self.move_to(100)

    async def move_stop(self):
        """Send command to stop the roller."""
        self.serial_position_at = None  # see move_to
        await self.hub.send_payload(
            {
                "method": "shadow",
                "args": {
                    "desired": {"shades": {self.id: {"stopShade": True}}},
                    "timeStamp": time.time(),
                },
            }
        )


def forcetoint(src) -> int:
    """Forces the input value to an int, on error, returns 0"""
    try:
        return int(src)
    except Exception:
        return 0

