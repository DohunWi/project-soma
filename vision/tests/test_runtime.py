import logging

from vision.runtime import (
    CameraManager,
    FrameResult,
    VisionSocketTransport,
    cleanup_resources,
    run_guarded,
    run_vision_loop,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def wall(self):
        return 1_700_000_000.0 + self.value

    def sleep(self, seconds):
        self.value += seconds

    def advance(self, seconds):
        self.value += seconds


class FakeSocket:
    def __init__(self, connect_results=None, emit_results=None):
        self.handlers = {}
        self.connect_results = list(connect_results or [True])
        self.emit_results = list(emit_results or [])
        self.connected = False
        self.emitted = []
        self.disconnect_calls = 0

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self, url, auth=None, wait_timeout=None):
        del url, auth, wait_timeout
        result = self.connect_results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.connected = True
        self.handlers["connect"]()

    def emit(self, event, payload):
        if self.emit_results:
            result = self.emit_results.pop(0)
            if isinstance(result, Exception):
                raise result
        self.emitted.append((event, payload))

    def disconnect(self):
        self.disconnect_calls += 1
        was_connected = self.connected
        self.connected = False
        if was_connected:
            self.handlers["disconnect"]()

    def runtime_disconnect(self):
        self.connected = False
        self.handlers["disconnect"]()


class FakeCapture:
    def __init__(self, opened=True, reads=None, release_error=None):
        self.opened = opened
        self.reads = list(reads or [])
        self.release_error = release_error
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        if self.reads:
            return self.reads.pop(0)
        return False, None

    def release(self):
        self.released = True
        if self.release_error:
            raise self.release_error


class CaptureFactory:
    def __init__(self, captures):
        self.captures = list(captures)
        self.calls = 0

    def __call__(self, index):
        del index
        self.calls += 1
        return self.captures.pop(0)


def make_camera(clock, captures, **kwargs):
    factory = CaptureFactory(captures)
    camera = CameraManager(
        factory,
        0,
        monotonic=clock.monotonic,
        **kwargs,
    )
    return camera, factory


def make_transport(clock, socket):
    return VisionSocketTransport(
        socket,
        "http://backend.invalid:5000",
        "not-logged",
        monotonic=clock.monotonic,
    )


def test_backend_unavailable_keeps_processing_and_drops_without_replay():
    clock = FakeClock()
    socket = FakeSocket([ConnectionError("offline"), True])
    transport = make_transport(clock, socket)
    camera, _ = make_camera(
        clock,
        [FakeCapture(reads=[(True, "a"), (True, "b")])],
    )
    transport.start()

    stats = run_vision_loop(
        camera,
        lambda frame, now: FrameResult([{"frame": frame, "t": now}]),
        transport,
        wall_clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        max_iterations=2,
    )

    assert stats.processed_frames == 2
    assert transport.dropped_payloads == 2
    assert socket.emitted == []

    clock.advance(1)
    transport.tick()
    assert transport.send({"frame": "new"}) is True
    assert socket.emitted == [("sensor_data", {"frame": "new"})]


def test_disconnected_payload_is_dropped():
    clock = FakeClock()
    transport = make_transport(clock, FakeSocket([ConnectionError()]))

    transport.start()

    assert transport.send({"seq": 1}) is False
    assert transport.dropped_payloads == 1


def test_runtime_disconnect_keeps_loop_alive_and_reconnects():
    clock = FakeClock()
    socket = FakeSocket([True, True])
    transport = make_transport(clock, socket)
    transport.start()
    socket.runtime_disconnect()

    assert transport.send({"seq": "old"}) is False
    clock.advance(1)
    transport.tick()
    assert transport.send({"seq": "fresh"}) is True
    assert socket.emitted == [("sensor_data", {"seq": "fresh"})]


def test_emit_exception_is_contained_and_schedules_reconnect(caplog):
    clock = FakeClock()
    socket = FakeSocket([True, True], [OSError("connection lost")])
    transport = make_transport(clock, socket)
    transport.start()

    with caplog.at_level(logging.ERROR):
        assert transport.send({"seq": 1}) is False

    assert "emit failed" in caplog.text
    assert transport.dropped_payloads == 1
    assert transport.connected is False
    clock.advance(1)
    transport.tick()
    assert transport.send({"seq": 2}) is True


def test_initial_camera_open_failure_retries_without_terminating():
    clock = FakeClock()
    failed = FakeCapture(opened=False)
    recovered = FakeCapture(reads=[(True, "frame")])
    camera, factory = make_camera(clock, [failed, recovered])

    assert camera.read() == (False, None)
    assert failed.released is True
    clock.advance(1)
    assert camera.read() == (True, "frame")
    assert factory.calls == 2


def test_single_camera_read_failure_retries_same_capture():
    clock = FakeClock()
    capture = FakeCapture(reads=[(False, None), (True, "frame")])
    camera, factory = make_camera(clock, [capture])

    assert camera.read() == (False, None)
    assert capture.released is False
    assert camera.read() == (True, "frame")
    assert factory.calls == 1


def test_ten_camera_failures_release_and_reopen():
    clock = FakeClock()
    broken = FakeCapture(reads=[(False, None)] * 10)
    recovered = FakeCapture(reads=[(True, "recovered")])
    camera, factory = make_camera(clock, [broken, recovered])

    for _ in range(10):
        assert camera.read() == (False, None)
    assert broken.released is True
    clock.advance(1)
    assert camera.read() == (True, "recovered")
    assert factory.calls == 2


def test_reopen_failure_uses_backoff_then_processing_resumes():
    clock = FakeClock()
    broken = FakeCapture(reads=[(False, None)] * 10)
    failed_reopen = FakeCapture(opened=False)
    recovered = FakeCapture(reads=[(True, "recovered")])
    camera, factory = make_camera(clock, [broken, failed_reopen, recovered])

    for _ in range(10):
        camera.read()
    clock.advance(1)
    assert camera.read() == (False, None)
    assert failed_reopen.released is True
    clock.advance(1.9)
    assert camera.read() == (False, None)
    clock.advance(0.1)
    assert camera.read() == (True, "recovered")
    assert factory.calls == 3


class RecordingTransport:
    connected = True
    dropped_payloads = 0

    def __init__(self):
        self.payloads = []

    def tick(self, force=False):
        del force

    def send(self, payload):
        self.payloads.append(payload)


def test_face_not_detected_is_a_normal_processed_frame():
    clock = FakeClock()
    camera, _ = make_camera(
        clock, [FakeCapture(reads=[(True, "no-face")])]
    )
    transport = RecordingTransport()

    stats = run_vision_loop(
        camera,
        lambda frame, now: FrameResult(
            [{"source": "vision", "face_detected": False, "t": now}]
        ),
        transport,
        wall_clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        max_iterations=1,
    )

    assert stats.processed_frames == 1
    assert stats.inference_errors == 0
    assert transport.payloads[0]["face_detected"] is False


def test_repeated_inference_exceptions_skip_frames_and_log_traceback(caplog):
    clock = FakeClock()
    camera, _ = make_camera(
        clock,
        [FakeCapture(reads=[(True, 1), (True, 2), (True, 3)])],
    )
    transport = RecordingTransport()
    calls = 0

    def process(frame, now):
        nonlocal calls
        del frame, now
        calls += 1
        if calls < 3:
            raise ValueError("inference exploded")
        return FrameResult([{"ok": True}])

    with caplog.at_level(logging.ERROR):
        stats = run_vision_loop(
            camera,
            process,
            transport,
            wall_clock=clock.wall,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            max_iterations=3,
        )

    assert stats.inference_errors == 2
    assert stats.processed_frames == 1
    assert transport.payloads == [{"ok": True}]
    assert "Traceback" in caplog.text
    assert "inference exploded" in caplog.text
    assert caplog.text.count("Vision inference failed") == 1


def test_keyboard_interrupt_runs_all_cleanup_and_is_normal_shutdown():
    cleaned = []

    def interrupt():
        raise KeyboardInterrupt

    code = run_guarded(
        interrupt,
        [
            ("camera", lambda: cleaned.append("camera")),
            ("detector", lambda: cleaned.append("detector")),
            ("socket", lambda: cleaned.append("socket")),
        ],
    )

    assert code == 0
    assert cleaned == ["camera", "detector", "socket"]


def test_cleanup_exception_does_not_hide_other_cleanup(caplog):
    cleaned = []

    def fail():
        raise RuntimeError("release failed")

    with caplog.at_level(logging.ERROR):
        cleanup_resources(
            [
                ("camera", fail),
                ("detector", lambda: cleaned.append("detector")),
                ("socket", lambda: cleaned.append("socket")),
            ]
        )

    assert cleaned == ["detector", "socket"]
    assert "release failed" in caplog.text


def test_normal_shutdown_releases_camera_detector_and_socket():
    clock = FakeClock()
    capture = FakeCapture(reads=[(True, "frame")])
    camera, _ = make_camera(clock, [capture])
    socket = FakeSocket([True])
    transport = make_transport(clock, socket)
    detector_closed = []
    camera.read()
    transport.start()

    code = run_guarded(
        lambda: None,
        [
            ("camera", camera.close),
            ("detector", lambda: detector_closed.append(True)),
            ("socket", transport.close),
        ],
    )

    assert code == 0
    assert capture.released is True
    assert detector_closed == [True]
    assert socket.disconnect_calls == 1


def test_fatal_unexpected_error_logs_traceback_and_cleans_up(caplog):
    cleaned = []

    def fail():
        raise RuntimeError("fatal")

    with caplog.at_level(logging.ERROR):
        code = run_guarded(
            fail,
            [("camera", lambda: cleaned.append("camera"))],
        )

    assert code == 1
    assert cleaned == ["camera"]
    assert "Traceback" in caplog.text
    assert "fatal unexpected error" in caplog.text
