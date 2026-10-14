#!/usr/bin/env python3
"""Optional Backend-to-Arduino-Nano Socket.IO and serial bridge.

The bridge translates validated logical Feedback Decisions into the small
stateful LEVEL / transition-only ALERT serial protocol. Serial and Backend
outages are retried locally and never participate in the Backend core path.
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


log = logging.getLogger("soma.feedback.nano")
LEVELS = frozenset({"NORMAL", "NOTICE", "WARNING", "BREAK"})
ALERT_LEVELS = frozenset({"WARNING", "BREAK"})
OFF_COMMAND = "LEVEL,OFF"
DEFAULT_BAUD_RATE = 9600
DEFAULT_POLL_INTERVAL_SEC = 0.05
DEFAULT_RECONNECT_INITIAL_SEC = 1.0
DEFAULT_RECONNECT_MAX_SEC = 10.0


def commands_for_decision(decision):
    """Map one logical decision to idempotent LEVEL and optional ALERT lines."""
    if not isinstance(decision, dict):
        raise ValueError("feedback decision must be an object")
    level = decision.get("level")
    transition = decision.get("transition")
    if level not in LEVELS or not isinstance(transition, bool):
        raise ValueError("feedback decision has invalid level or transition")
    if level == "NORMAL" and decision.get("reason") == "ABSENT":
        return [OFF_COMMAND]

    commands = [f"LEVEL,{level}"]
    if transition and level in ALERT_LEVELS:
        commands.append(f"ALERT,{level}")
    return commands


class NanoSerialTransport:
    """Reconnectable non-blocking serial transport with LEVEL-only replay."""

    def __init__(
        self,
        port,
        baud_rate=DEFAULT_BAUD_RATE,
        *,
        serial_factory,
        monotonic=time.monotonic,
        reconnect_initial_sec=DEFAULT_RECONNECT_INITIAL_SEC,
        reconnect_max_sec=DEFAULT_RECONNECT_MAX_SEC,
    ):
        self.port = port
        self.baud_rate = baud_rate
        self._serial_factory = serial_factory
        self._monotonic = monotonic
        self._reconnect_initial_sec = reconnect_initial_sec
        self._reconnect_max_sec = reconnect_max_sec
        self._reconnect_delay_sec = reconnect_initial_sec
        self._next_open_at = 0.0
        self._serial = None
        self._ready = False
        self.latest_level = OFF_COMMAND

    @property
    def connected(self):
        return self._serial is not None

    @property
    def ready(self):
        return self._serial is not None and self._ready

    def poll(self):
        """Attempt reconnect and consume all currently available Nano lines."""
        now = self._monotonic()
        if self._serial is None:
            self._open_if_due(now)
        if self._serial is None:
            return
        try:
            while getattr(self._serial, "in_waiting", 0) > 0:
                raw = self._serial.readline()
                line = raw.decode("ascii", errors="ignore").strip()
                if line == "READY":
                    self._ready = True
                    self._write(self.latest_level)
                elif line:
                    log.debug("Nano input ignored: %r", line)
        except Exception as error:  # noqa: BLE001
            self._mark_disconnected(now, error)

    def send(self, commands):
        """Cache LEVEL and best-effort each command once; ALERT is never queued."""
        for command in commands:
            if command.startswith("LEVEL,"):
                self.latest_level = command
            elif not command.startswith("ALERT,"):
                raise ValueError(f"unsupported Nano command: {command!r}")

            if not self.ready:
                continue
            try:
                self._write(command)
            except Exception as error:  # noqa: BLE001
                self._mark_disconnected(self._monotonic(), error)
                # An ALERT write may have reached the device; never retry it.
                break

    def close(self):
        """Best-effort OFF, then release the serial device."""
        self.send([OFF_COMMAND])
        self._close_serial()

    def _open_if_due(self, now):
        if now < self._next_open_at:
            return
        try:
            self._serial = self._serial_factory(
                port=self.port,
                baudrate=self.baud_rate,
                timeout=0,
            )
            self._ready = False
            self._reconnect_delay_sec = self._reconnect_initial_sec
            log.info("Feedback Nano serial connected: %s", self.port)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "Feedback Nano serial unavailable (%s)",
                type(error).__name__,
            )
            self._schedule_reconnect(now)

    def _write(self, command):
        self._serial.write(f"{command}\n".encode("ascii"))

    def _mark_disconnected(self, now, error):
        log.warning("Feedback Nano serial lost (%s)", type(error).__name__)
        self._close_serial()
        self._schedule_reconnect(now)

    def _schedule_reconnect(self, now):
        self._next_open_at = now + self._reconnect_delay_sec
        self._reconnect_delay_sec = min(
            self._reconnect_delay_sec * 2,
            self._reconnect_max_sec,
        )

    def _close_serial(self):
        serial_connection = self._serial
        self._serial = None
        self._ready = False
        if serial_connection is not None:
            try:
                serial_connection.close()
            except Exception as error:  # noqa: BLE001
                log.warning("Feedback Nano serial close failed (%s)", type(error).__name__)


class NanoBridge:
    """Coordinate a reconnectable Socket.IO client and Nano serial transport."""

    def __init__(
        self,
        url,
        token,
        serial_transport,
        socket_client,
        *,
        monotonic=time.monotonic,
        sleeper=time.sleep,
        reconnect_initial_sec=DEFAULT_RECONNECT_INITIAL_SEC,
        reconnect_max_sec=DEFAULT_RECONNECT_MAX_SEC,
    ):
        self.url = url
        self._token = token
        self.serial = serial_transport
        self.socket = socket_client
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._reconnect_initial_sec = reconnect_initial_sec
        self._reconnect_max_sec = reconnect_max_sec
        self._reconnect_delay_sec = reconnect_initial_sec
        self._next_backend_attempt = 0.0
        self._register_handlers()

    def _register_handlers(self):
        self.socket.on("connect", self._on_connect)
        self.socket.on("disconnect", self._on_disconnect)
        self.socket.on("feedback_device", self._on_feedback)
        self.socket.on("feedback_device_off", self._on_off)

    def _on_connect(self):
        self._reconnect_delay_sec = self._reconnect_initial_sec
        self._next_backend_attempt = 0.0
        log.info("Feedback Backend connected: %s", self.url)

    def _on_disconnect(self):
        self._next_backend_attempt = self._monotonic()
        log.warning("Feedback Backend disconnected; retrying")

    def _on_feedback(self, decision):
        try:
            self.serial.send(commands_for_decision(decision))
        except (TypeError, ValueError) as error:
            log.warning("Invalid feedback device event ignored: %s", error)

    def _on_off(self, _payload=None):
        self.serial.send([OFF_COMMAND])

    def tick(self):
        """Advance both reconnect loops once without sleeping."""
        self.serial.poll()
        if getattr(self.socket, "connected", False):
            return
        now = self._monotonic()
        if now < self._next_backend_attempt:
            return
        try:
            self.socket.connect(
                self.url,
                auth={"token": self._token, "role": "feedback_device"},
                wait_timeout=2,
            )
        except Exception as error:  # noqa: BLE001
            log.warning("Feedback Backend unavailable (%s)", type(error).__name__)
            self._next_backend_attempt = now + self._reconnect_delay_sec
            self._reconnect_delay_sec = min(
                self._reconnect_delay_sec * 2,
                self._reconnect_max_sec,
            )

    def run(self, stop_event, poll_interval_sec=DEFAULT_POLL_INTERVAL_SEC):
        while not stop_event.is_set():
            self.tick()
            self._sleeper(poll_interval_sec)

    def close(self):
        self.serial.close()
        if getattr(self.socket, "connected", False):
            try:
                self.socket.disconnect()
            except Exception as error:  # noqa: BLE001
                log.warning("Feedback Backend disconnect failed (%s)", type(error).__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=os.getenv("FEEDBACK_SERIAL_PORT"))
    parser.add_argument(
        "--baud",
        type=int,
        default=int(os.getenv("FEEDBACK_BAUD_RATE", DEFAULT_BAUD_RATE)),
    )
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}",
    )
    args = parser.parse_args()
    if not args.port:
        parser.error("FEEDBACK_SERIAL_PORT 또는 --port가 필요합니다")
    token = os.getenv("SOCKET_AUTH_TOKEN")
    if not token:
        parser.error("SOCKET_AUTH_TOKEN이 필요합니다")

    try:
        import serial
        import socketio
    except ImportError:
        sys.exit("feedback dependencies가 없습니다: pip install -r feedback/requirements.txt")

    logging.basicConfig(level=logging.INFO, format="[nano] %(message)s")
    serial_transport = NanoSerialTransport(
        args.port,
        args.baud,
        serial_factory=serial.Serial,
    )
    bridge = NanoBridge(
        args.url,
        token,
        serial_transport,
        socketio.Client(reconnection=False, logger=False, engineio_logger=False),
    )
    stop_event = threading.Event()

    def request_stop(*_args):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        bridge.run(stop_event)
    finally:
        bridge.close()
        log.info("Feedback Nano bridge stopped")


if __name__ == "__main__":
    main()
