from tools.demo.run_all import (
    PY,
    find_core_exit,
    load_project_env,
    process_specs,
)


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_vision_exit_does_not_select_core_shutdown(capsys):
    handled = set()
    server = FakeProcess()
    vision = FakeProcess(1)

    assert find_core_exit(
        [("server", server), ("vision", vision)], handled
    ) is None
    assert handled == {"vision"}
    assert "나머지 시스템은 계속" in capsys.readouterr().out


def test_nano_exit_does_not_select_core_shutdown(capsys):
    handled = set()
    server = FakeProcess()
    nano = FakeProcess(1)

    assert find_core_exit(
        [("server", server), ("nano", nano)], handled
    ) is None
    assert handled == {"nano"}
    assert "나머지 시스템은 계속" in capsys.readouterr().out


def test_core_exit_preserves_shutdown_semantics():
    server = FakeProcess(2)

    assert find_core_exit([("server", server)], set()) == ("server", server)


def test_mock_exit_remains_a_core_failure():
    mock = FakeProcess(3)

    assert find_core_exit([("mock", mock)], set()) == ("mock", mock)


def test_default_processes_do_not_start_legacy_feedback():
    specs = process_specs(real=False, chair_only=False, nano=False)

    assert [name for name, _command in specs] == ["server", "mock"]
    flattened = [part for _name, command in specs for part in command]
    assert "feedback/policy/policy.py" not in flattened
    assert "feedback/ambient_led/driver.py" not in flattened


def test_real_chair_only_can_enable_nano_and_raw_logging():
    specs = process_specs(
        real=True,
        chair_only=True,
        nano=True,
        chair_raw_log="logs/chair.jsonl",
    )

    assert specs == [
        ("server", [PY, "server/app.py"]),
        (
            "chair",
            [
                PY,
                "chair/bridge/bridge.py",
                "--raw-log",
                "logs/chair.jsonl",
            ],
        ),
        ("nano", [PY, "feedback/nano/bridge.py"]),
    ]


def test_project_env_is_loaded_for_child_processes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SOCKET_AUTH_TOKEN=test-shared-token\n", encoding="utf-8")
    monkeypatch.delenv("SOCKET_AUTH_TOKEN", raising=False)

    assert load_project_env(env_file) is True
    assert __import__("os").environ["SOCKET_AUTH_TOKEN"] == "test-shared-token"
