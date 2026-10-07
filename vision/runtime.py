"""Failure-tolerant runtime helpers for the Vision producer.

The analysis code deliberately lives outside this module.  These helpers only
manage transient socket/camera failures, the frame loop, logging, and cleanup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


CAMERA_READ_FAILURE_LIMIT = 10
CAMERA_REOPEN_BACKOFF_SEC = (1.0, 2.0, 5.0)
SOCKET_RETRY_BACKOFF_SEC = (1.0, 2.0, 5.0)
HEARTBEAT_INTERVAL_SEC = 15.0
ERROR_LOG_INTERVAL_SEC = 10.0

LOG = logging.getLogger("soma.vision")


def _backoff_value(values: Sequence[float], index: int) -> float:
    return values[min(index, len(values) - 1)]


class VisionSocketTransport:
    """Best-effort realtime transport with no disconnected payload queue."""

    def __init__(
        self,
        client: Any,
        url: str,
        token: str | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        retry_backoff_sec: Sequence[float] = SOCKET_RETRY_BACKOFF_SEC,
        logger: logging.Logger = LOG,
    ) -> None:
        self._client = client
        self._url = url
        self._token = token
        self._monotonic = monotonic
        self._backoff = tuple(retry_backoff_sec)
        self._logger = logger
        self._connected = False
        self._closed = False
        self._next_retry_at = 0.0
        self._retry_index = 0
        self._connect_attempts = 0
        self._last_emit_error_log: float | None = None
        self.dropped_payloads = 0

        client.on("connect", self._on_connect)
        client.on("disconnect", self._on_disconnect)

    @property
    def connected(self) -> bool:
        return self._connected

    def _on_connect(self, *_: Any) -> None:
        self._connected = True
        self._retry_index = 0
        self._logger.info("Vision backend connected")

    def _on_disconnect(self, *_: Any) -> None:
        was_connected = self._connected
        self._connected = False
        self._next_retry_at = self._monotonic() + self._backoff[0]
        self._retry_index = 1
        if was_connected:
            self._logger.warning("Vision backend disconnected; retry scheduled")

    def start(self) -> None:
        self.tick(force=True)

    def tick(self, *, force: bool = False) -> None:
        if self._closed or self._connected:
            return
        now = self._monotonic()
        if not force and now < self._next_retry_at:
            return

        self._connect_attempts += 1
        action = "connecting" if self._connect_attempts == 1 else "reconnecting"
        log_attempt = self._connect_attempts == 1 or self._connect_attempts % 12 == 0
        if log_attempt:
            self._logger.info("Vision backend %s: %s", action, self._url)
        try:
            auth = {"token": self._token} if self._token else None
            self._client.connect(self._url, auth=auth, wait_timeout=1)
            if getattr(self._client, "connected", False) and not self._connected:
                self._on_connect()
        except Exception as exc:
            delay = _backoff_value(self._backoff, self._retry_index)
            self._retry_index += 1
            self._next_retry_at = now + delay
            if log_attempt:
                self._logger.warning(
                    "Vision backend unavailable (%s); retry in %.1fs",
                    type(exc).__name__,
                    delay,
                )

    def send(self, payload: dict[str, Any]) -> bool:
        if not self._connected:
            self._record_drop("backend disconnected")
            return False
        try:
            self._client.emit("sensor_data", payload)
            return True
        except Exception:
            now = self._monotonic()
            if (
                self._last_emit_error_log is None
                or now - self._last_emit_error_log >= ERROR_LOG_INTERVAL_SEC
            ):
                self._logger.exception("Vision payload emit failed; reconnect scheduled")
                self._last_emit_error_log = now
            self._record_drop("emit failure")
            self._connected = False
            self._next_retry_at = now + self._backoff[0]
            self._retry_index = 1
            self._disconnect_client(log_failure=False)
            return False

    def _record_drop(self, reason: str) -> None:
        self.dropped_payloads += 1
        if self.dropped_payloads == 1 or self.dropped_payloads % 100 == 0:
            self._logger.warning(
                "Vision payload dropped (%s); total=%d",
                reason,
                self.dropped_payloads,
            )

    def _disconnect_client(self, *, log_failure: bool) -> None:
        try:
            self._client.disconnect()
        except Exception:
            if log_failure:
                self._logger.exception("Vision socket cleanup failed")

    def close(self) -> None:
        self._closed = True
        self._connected = False
        self._disconnect_client(log_failure=True)


class StdoutTransport:
    """Transport-compatible JSONL output used by ``vision/run.py --stdout``."""

    connected = True
    dropped_payloads = 0

    def __init__(self, write: Callable[[dict[str, Any]], None]) -> None:
        self._write = write

    def start(self) -> None:
        return None

    def tick(self, *, force: bool = False) -> None:
        del force

    def send(self, payload: dict[str, Any]) -> bool:
        self._write(payload)
        return True

    def close(self) -> None:
        return None


class CameraManager:
    """Own and recover a camera capture without terminating the producer."""

    def __init__(
        self,
        capture_factory: Callable[[int], Any],
        camera_index: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        reopen_backoff_sec: Sequence[float] = CAMERA_REOPEN_BACKOFF_SEC,
        read_failure_limit: int = CAMERA_READ_FAILURE_LIMIT,
        logger: logging.Logger = LOG,
    ) -> None:
        self._capture_factory = capture_factory
        self._camera_index = camera_index
        self._monotonic = monotonic
        self._backoff = tuple(reopen_backoff_sec)
        self._read_failure_limit = read_failure_limit
        self._logger = logger
        self._capture: Any | None = None
        self._next_open_at = 0.0
        self._backoff_index = 0
        self._open_attempts = 0
        self.consecutive_read_failures = 0
        self.reopen_count = 0

    @property
    def status(self) -> str:
        return "active" if self._capture is not None else "retrying"

    def read(self) -> tuple[bool, Any | None]:
        if self._capture is None and not self._open_if_due():
            return False, None

        try:
            ok, frame = self._capture.read()
        except Exception:
            self._logger.exception("Camera read raised an exception")
            ok, frame = False, None

        if ok:
            self.consecutive_read_failures = 0
            return True, frame

        self.consecutive_read_failures += 1
        if self.consecutive_read_failures == 1:
            self._logger.warning("Camera read failed; retrying")
        if self.consecutive_read_failures >= self._read_failure_limit:
            self._logger.error(
                "Camera read failed %d consecutive times; reopening",
                self.consecutive_read_failures,
            )
            self._release_capture()
            self.consecutive_read_failures = 0
            self.reopen_count += 1
            self._schedule_open()
        return False, None

    def _open_if_due(self) -> bool:
        now = self._monotonic()
        if now < self._next_open_at:
            return False

        self._open_attempts += 1
        candidate = None
        try:
            candidate = self._capture_factory(self._camera_index)
            if candidate is not None and candidate.isOpened():
                self._capture = candidate
                self._backoff_index = 0
                self._logger.info("Camera %d opened", self._camera_index)
                return True
        except Exception:
            self._logger.exception("Camera %d open failed", self._camera_index)

        self._safe_release(candidate)
        self.reopen_count += int(self._open_attempts > 1)
        self._schedule_open()
        return False

    def _schedule_open(self) -> None:
        delay = _backoff_value(self._backoff, self._backoff_index)
        self._backoff_index += 1
        self._next_open_at = self._monotonic() + delay
        if self._open_attempts == 1 or self._open_attempts % 12 == 0:
            self._logger.warning(
                "Camera %d unavailable; reopen in %.1fs",
                self._camera_index,
                delay,
            )

    def _safe_release(self, capture: Any | None) -> None:
        if capture is None:
            return
        try:
            capture.release()
        except Exception:
            self._logger.exception("Camera release failed")

    def _release_capture(self) -> None:
        capture, self._capture = self._capture, None
        self._safe_release(capture)

    def close(self) -> None:
        self._release_capture()


@dataclass
class FrameResult:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    stop: bool = False


@dataclass
class LoopStats:
    captured_frames: int = 0
    processed_frames: int = 0
    inference_errors: int = 0
    last_success_at: float | None = None


def run_vision_loop(
    camera: CameraManager,
    process_frame: Callable[[Any, float], FrameResult],
    transport: Any,
    *,
    wall_clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] = lambda: False,
    heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
    max_iterations: int | None = None,
    logger: logging.Logger = LOG,
) -> LoopStats:
    """Run the producer loop while isolating transient frame-level failures."""

    stats = LoopStats()
    iteration = 0
    last_heartbeat = monotonic()
    last_inference_error_log: float | None = None

    while not stop_requested():
        if max_iterations is not None and iteration >= max_iterations:
            break
        iteration += 1
        transport.tick()
        ok, frame = camera.read()
        now_mono = monotonic()
        if not ok:
            _log_heartbeat(
                camera, transport, stats, now_mono, last_heartbeat,
                heartbeat_interval_sec, logger,
            )
            if now_mono - last_heartbeat >= heartbeat_interval_sec:
                last_heartbeat = now_mono
            sleep(0.03)
            continue

        stats.captured_frames += 1
        stats.last_success_at = now_mono
        try:
            result = process_frame(frame, wall_clock())
        except Exception:
            stats.inference_errors += 1
            if (
                last_inference_error_log is None
                or now_mono - last_inference_error_log >= ERROR_LOG_INTERVAL_SEC
            ):
                logger.exception("Vision inference failed; frame skipped")
                last_inference_error_log = now_mono
            result = FrameResult()
        else:
            stats.processed_frames += 1

        for payload in result.payloads:
            transport.send(payload)
        if result.stop:
            break

        if now_mono - last_heartbeat >= heartbeat_interval_sec:
            _log_heartbeat(
                camera, transport, stats, now_mono, last_heartbeat,
                heartbeat_interval_sec, logger,
            )
            last_heartbeat = now_mono

    return stats


def _log_heartbeat(
    camera: CameraManager,
    transport: Any,
    stats: LoopStats,
    now: float,
    last_heartbeat: float,
    interval: float,
    logger: logging.Logger,
) -> None:
    if now - last_heartbeat < interval:
        return
    frame_age = None
    if stats.last_success_at is not None:
        frame_age = max(0.0, now - stats.last_success_at)
    logger.info(
        "Vision health camera=%s backend=%s processed=%d "
        "last_frame_age_sec=%s dropped=%d reopens=%d inference_errors=%d",
        camera.status,
        "connected" if transport.connected else "disconnected",
        stats.processed_frames,
        "never" if frame_age is None else f"{frame_age:.1f}",
        transport.dropped_payloads,
        camera.reopen_count,
        stats.inference_errors,
    )


def cleanup_resources(
    cleanups: Iterable[tuple[str, Callable[[], None]]],
    *,
    logger: logging.Logger = LOG,
) -> None:
    for name, cleanup in cleanups:
        try:
            cleanup()
        except Exception:
            logger.exception("Vision cleanup failed: %s", name)


def run_guarded(
    run: Callable[[], None],
    cleanups: Iterable[tuple[str, Callable[[], None]]],
    *,
    logger: logging.Logger = LOG,
) -> int:
    reason = "completed"
    exit_code = 0
    try:
        run()
    except KeyboardInterrupt:
        reason = "user interrupt"
    except BrokenPipeError:
        reason = "output pipe closed"
    except Exception:
        reason = "fatal unexpected error"
        exit_code = 1
        logger.exception("Vision fatal unexpected error")
    finally:
        cleanup_resources(cleanups, logger=logger)
    logger.info("Vision shutdown: %s; cleanup complete", reason)
    return exit_code
