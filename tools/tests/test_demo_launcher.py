"""Local demo orchestration without credentials, devices or DB writes."""
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from tools.demo import run_all as launcher


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name in (
        "SOMA_MODE", "SOMA_SOURCE_MODE", "SOMA_STATIC_POLICY", "SOMA_BLINK_POLICY",
        "CHAIR_SERIAL_PORT", "FEEDBACK_SERIAL_PORT", "SUPABASE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SOCKET_AUTH_TOKEN"):
        monkeypatch.setenv(name, "sensitive-test-value")
    front = tmp_path / "front"
    for name in ("tools/serve_front.py", "web/view/login.html", "web/view/signup.html",
                 "web/view/index.html", "web/view/auth.js", "web/view/measurement_runtime.mjs",
                 "web/view/measurement_phase.mjs"):
        path = front / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(launcher, "load_project_env", lambda: None)
    return ["--front-root", str(front)]


@pytest.mark.parametrize("mode", ["demo", "normal"])
@pytest.mark.parametrize("source", ["all", "chair-only"])
def test_modes_and_wall_clock_mock(configured, mode, source):
    args = launcher.build_parser().parse_args(
        configured + ["--mode", mode, "--source-mode", source, "--mock"])
    specs = launcher.prepare(args)
    env = launcher.child_environment(args)
    assert env["SOMA_MODE"] == mode
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "5000"
    assert env["CORS_ALLOWED_ORIGINS"] == (
        "http://127.0.0.1:5500,http://127.0.0.1:5000"
    )
    server_command = specs[0][1]
    assert server_command[server_command.index("--source-mode") + 1] == source
    expected_static = "cumulative-v2" if mode == "normal" else "legacy"
    expected_blink = "score-v1" if mode == "normal" else "legacy"
    assert server_command[server_command.index("--static-policy") + 1] == expected_static
    assert server_command[server_command.index("--blink-policy") + 1] == expected_blink
    assert specs[1][1][-6:] == ["--speed", "1", "--hz", "1", "--source",
                              "chair" if source == "chair-only" else "all"]
    assert specs[1][1][specs[1][1].index("--scenario") + 1] == "normal"


def test_default_no_producer_or_measurement_request(configured, monkeypatch):
    monkeypatch.setenv("SOMA_MODE", "normal")
    args = launcher.build_parser().parse_args(configured)
    assert [name for name, _ in launcher.prepare(args)] == ["server"]
    assert args.mode == "normal"
    # Neither CLI runner that owns start/stop is part of the command plan.
    assert not any("chair_e2e" in part or "cross_validation" in part
                   for _, command in launcher.prepare(args) for part in command)


def test_normal_legacy_environment_override_is_forwarded(configured, monkeypatch):
    monkeypatch.setenv("SOMA_STATIC_POLICY", "legacy")
    monkeypatch.setenv("SOMA_BLINK_POLICY", "legacy")
    args = launcher.build_parser().parse_args(configured + ["--mode", "normal"])

    server_command = launcher.prepare(args)[0][1]

    assert args.static_policy == "legacy"
    assert args.blink_policy == "legacy"
    assert server_command[-4:] == [
        "--static-policy", "legacy", "--blink-policy", "legacy",
    ]


def test_cli_policy_overrides_environment_and_is_reported(
    configured,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("SOMA_STATIC_POLICY", "legacy")
    monkeypatch.setenv("SOMA_BLINK_POLICY", "legacy")
    monkeypatch.setattr(launcher, "check_port", lambda _port: None)
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))

    assert launcher.main(configured + [
        "--mode", "normal",
        "--static-policy", "cumulative-v2",
        "--blink-policy", "score-v1",
        "--check-only",
    ]) == 0

    output = capsys.readouterr().out
    assert "적용 정책: static=cumulative-v2, blink=score-v1" in output


@pytest.mark.parametrize(
    "options",
    [
        ["--mode", "demo", "--static-policy", "cumulative-v2"],
        ["--mode", "demo", "--blink-policy", "score-v1"],
    ],
)
def test_demo_rejects_normal_only_policy_options(
    configured,
    options,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))

    assert launcher.main(configured + options) == 1
    assert "Demo" in capsys.readouterr().err


def test_real_and_explicit_hardware_selection(configured):
    args = launcher.build_parser().parse_args(configured + ["--real", "--nano",
        "--chair-port", "COM3", "--nano-port", "COM4", "--camera-index", "2",
        "--chair-raw-log", "logs/new.jsonl"])
    specs = launcher.prepare(args)
    assert [name for name, _ in specs] == ["server", "chair", "vision", "nano"]
    assert specs[1][1][-4:] == ["--port", "COM3", "--raw-log", "logs/new.jsonl"]
    assert specs[2][1][-2:] == ["--cam", "2"]
    assert specs[-1][1][-2:] == ["--port", "COM4"]
    args = launcher.build_parser().parse_args(configured + ["--chair-only", "--chair"])
    assert [name for name, _ in launcher.prepare(args)] == ["server", "chair"]


@pytest.mark.parametrize("options", [
    ["--mock", "--chair"], ["--mock", "--vision"], ["--chair-only", "--vision"],
    ["--chair-port", "COM3"], ["--chair-raw-log", "logs/raw.jsonl"],
    ["--nano-port", "COM4"], ["--nano"], ["--camera-index", "0"],
    ["--vision", "--camera-index", "-1"], ["--web-port", "5000"],
    ["--web-port", "0"], ["--real", "--nano", "--nano-port", "COM4"],
    ["--chair", "--nano", "--chair-port", "COM4", "--nano-port", "com4"],
])
def test_bad_options_rejected_before_spawn(configured, options, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert launcher.main(configured + options) == 1
    assert "sensitive-test-value" not in capsys.readouterr().err


def test_missing_auth_does_not_echo_credentials_or_start(configured, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert launcher.main(configured) == 1
    output = capsys.readouterr()
    assert "SUPABASE_URL" in output.err
    assert "sensitive-test-value" not in output.out + output.err


def test_missing_front_never_falls_back_to_dashboard(configured, monkeypatch, capsys):
    (Path(configured[1]) / "tools/serve_front.py").unlink()
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert launcher.main(configured) == 1
    assert "Front" in capsys.readouterr().err


def test_port_conflict_leaves_other_service_untouched():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(launcher.LaunchError, match="Port"):
            launcher.check_port(listener.getsockname()[1])
        assert listener.fileno() != -1


def test_check_only_starts_nothing(configured, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "check_port", lambda p: None)
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert launcher.main(configured + ["--mock", "--check-only"]) == 0
    output = capsys.readouterr().out
    assert "자동 Start는 하지 않습니다" in output
    assert "Feedback Nano: 미실행" in output
    assert "sensitive-test-value" not in output


def test_nano_enabled_does_not_report_it_as_omitted(configured, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "check_port", lambda p: None)
    monkeypatch.setattr(launcher, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert launcher.main(configured + [
        "--nano", "--nano-port", "COM4", "--check-only",
    ]) == 0
    output = capsys.readouterr().out
    assert "구성: server, nano, front" in output
    assert "Feedback Nano: 미실행" not in output


class Process:
    def __init__(self, timeout=False):
        self.returncode = None
        self.actions = []
        self.timeout = timeout

    def poll(self):
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")

    def kill(self):
        self.actions.append("kill")

    def wait(self, timeout):
        self.actions.append("wait")
        if self.timeout:
            self.timeout = False
            raise subprocess.TimeoutExpired("fixture", timeout)
        self.returncode = 0


def test_ctrl_c_reaps_children_with_correct_front_command(configured, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    children, calls, probes = [], [], []

    def popen(command, cwd, env):
        child = Process()
        children.append(child)
        calls.append((command, cwd, env))
        return child

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "check_port", lambda p: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "wait_http", lambda p, url, **kw: probes.append((url, kw)))
    monkeypatch.setattr(launcher.time, "sleep", interrupt)
    assert launcher.main(configured) == 0
    assert len(children) == 2
    assert all(child.actions == ["terminate", "wait"] for child in children)
    assert calls[1][0] == [launcher.PY, str(Path(configured[1]) / "tools/serve_front.py"),
                            "--host", "127.0.0.1", "--port", "5500"]
    assert calls[1][1] == Path(configured[1])
    assert probes[-1] == ("http://127.0.0.1:5500/view/measurement_runtime.mjs",
                          {"mime": "text/javascript"})


def test_partial_startup_failure_also_cleans_up(configured, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    child = Process(timeout=True)
    monkeypatch.setattr(launcher, "check_port", lambda p: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: child)

    def fail(*a, **kw):
        raise launcher.LaunchError("startup failed")

    monkeypatch.setattr(launcher, "wait_http", fail)
    assert launcher.main(configured) == 1
    assert child.actions == ["terminate", "wait", "kill", "wait"]


def test_shutdown_file_requests_owned_child_cleanup(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    children = []

    def popen(*_args, **_kwargs):
        child = Process()
        children.append(child)
        return child

    shutdown_file = tmp_path / "shutdown.flag"
    shutdown_file.touch()
    monkeypatch.setattr(launcher, "check_port", lambda _port: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "wait_http", lambda *_args, **_kwargs: None)

    assert launcher.main(configured + ["--shutdown-file", str(shutdown_file)]) == 0
    assert len(children) == 2
    assert all(child.actions == ["terminate", "wait"] for child in children)


def test_optional_chair_exit_does_not_stop_front():
    chair, server = Process(), Process()
    chair.returncode = 1
    assert launcher.find_core_exit([("chair", chair), ("server", server)], set()) is None


def test_windows_cleanup_targets_only_owned_process_tree(monkeypatch):
    child = Process()
    child.pid = 12345
    calls = []
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    def taskkill(command, **kwargs):
        calls.append(command)
        child.returncode = 1
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", taskkill)
    launcher.stop_processes([("server", child)])
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
    assert child.actions == ["wait"]


@pytest.mark.parametrize("mode,source", [("demo", "chair-only"), ("normal", "all")])
def test_real_backend_and_front_without_hardware_db_or_login(monkeypatch, mode, source):
    """Exercise real child startup/HTTP/shutdown; never start a measurement."""
    front = launcher.ROOT.with_name("project-soma-front")
    if not (front / "tools/serve_front.py").is_file():
        pytest.skip("actual sibling Front worktree is not available")
    # Explicit blanks prevent the child Backend's dotenv from loading real DB credentials.
    for name in ("DB_HOST", "DB_USER", "DB_PASSWORD", "SUPABASE_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "synthetic-unused-public-key")
    monkeypatch.setenv("SOCKET_AUTH_TOKEN", "synthetic-unused-device-token")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(launcher, "load_project_env", lambda: None)
    with socket.socket() as backend_socket, socket.socket() as front_socket:
        backend_socket.bind(("127.0.0.1", 0))
        front_socket.bind(("127.0.0.1", 0))
        backend_port = backend_socket.getsockname()[1]
        front_port = front_socket.getsockname()[1]
    monkeypatch.setattr(launcher, "BACKEND_PORT", backend_port)
    observed = []

    def verify_then_interrupt(processes, handled):
        assert [name for name, _ in processes] == ["server", "front"]
        observed.extend(processes)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{backend_port}/api/health") as response:
            assert b'"status":"ok"' in response.read()
        # Missing auth must still fail, even in this software-only launch.
        request = urllib.request.Request(
            f"http://127.0.0.1:{backend_port}/api/measurement/start", data=b"{}",
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as rejected:
            opener.open(request)
        assert rejected.value.code == 401
        rejected.value.close()
        with opener.open(f"http://127.0.0.1:{front_port}/view/measurement_runtime.mjs") as response:
            assert response.headers.get_content_type() == "text/javascript"
            assert b"createMeasurementRuntime" in response.read()
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "find_core_exit", verify_then_interrupt)
    assert launcher.main(["--front-root", str(front), "--mode", mode,
                          "--source-mode", source, "--web-port", str(front_port)]) == 0
    assert len(observed) == 2
    assert all(process.poll() is not None for _, process in observed)
    # Windows venv redirectors must not leave a real server process behind.
    for port in (backend_port, front_port):
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
