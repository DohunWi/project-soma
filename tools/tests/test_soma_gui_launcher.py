import os
from pathlib import Path

import pytest

from tools.launcher.soma_launcher import (
    LauncherConfig,
    PreflightError,
    SingleInstance,
    build_run_all_command,
    validate_paths,
)


def make_config(tmp_path, **overrides):
    backend = tmp_path / "backend"
    (backend / "tools/demo").mkdir(parents=True)
    (backend / "tools/demo/run_all.py").write_text("", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    front = tmp_path / "front"
    from tools.launcher.soma_launcher import FRONT_FILES
    for name in FRONT_FILES:
        path = front / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    values = dict(
        backend_root=backend,
        python_path=python,
        front_root=front,
        mode="normal",
        use_mock=False,
        use_vision=True,
        use_nano=True,
        chair_port="COM3",
        nano_port="COM4",
        camera_index=2,
    )
    values.update(overrides)
    return LauncherConfig(**values)


def test_actual_normal_command_forwards_selected_devices(tmp_path):
    config = make_config(tmp_path)
    command = build_run_all_command(config, shutdown_file=Path("stop.flag"))

    assert command[command.index("--mode") + 1] == "normal"
    assert command[command.index("--source-mode") + 1] == "all"
    assert command[command.index("--chair-port") + 1] == "COM3"
    assert command[command.index("--nano-port") + 1] == "COM4"
    assert command[command.index("--camera-index") + 1] == "2"
    assert command[-2:] == ["--shutdown-file", "stop.flag"]


def test_mock_command_starts_no_real_device(tmp_path):
    command = build_run_all_command(make_config(tmp_path, mode="demo", use_mock=True))

    assert "--mock" in command
    assert "--chair" not in command
    assert "--vision" not in command
    assert "--nano" not in command


def test_chair_only_is_derived_when_vision_is_disabled(tmp_path):
    command = build_run_all_command(
        make_config(tmp_path, use_vision=False, use_nano=False, nano_port="")
    )
    assert command[command.index("--source-mode") + 1] == "chair-only"
    assert "--camera-index" not in command


def test_same_serial_port_is_rejected(tmp_path):
    config = make_config(tmp_path, nano_port="com3")
    with pytest.raises(PreflightError, match="서로 다른"):
        validate_paths(config)


def test_missing_front_file_is_rejected(tmp_path):
    config = make_config(tmp_path)
    (config.front_root / "web/view/auth.js").unlink()
    with pytest.raises(PreflightError, match="Front"):
        validate_paths(config)


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_mutex_blocks_duplicate():
    first = SingleInstance()
    try:
        with pytest.raises(RuntimeError, match="이미 실행"):
            SingleInstance()
    finally:
        first.close()

    recovered = SingleInstance()
    recovered.close()
