from tools.demo.run_all import find_core_exit


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
