"""Static safety checks for firmware when Arduino CLI is unavailable."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = (
    ROOT / "feedback" / "nano" / "firmware" / "feedback_nano.ino"
).read_text(encoding="utf-8")


def test_firmware_protocol_and_ready_handshake_are_present():
    for command in (
        "LEVEL,NORMAL",
        "LEVEL,NOTICE",
        "LEVEL,WARNING",
        "LEVEL,BREAK",
        "LEVEL,OFF",
        "ALERT,WARNING",
        "ALERT,BREAK",
        "READY",
    ):
        assert command in FIRMWARE


def test_firmware_is_non_blocking_and_has_finite_vibration_shutdown():
    assert "delay(" not in FIRMWARE
    assert "millis()" in FIRMWARE
    assert "stopVibration();" in FIRMWARE
    assert "digitalWrite(PIN_VIBRATION, LOW);" in FIRMWARE
    assert "COMMAND_TIMEOUT_MS" in FIRMWARE


def test_firmware_marks_unverified_wiring_as_hardware_todo():
    assert "TODO(hardware E2E)" in FIRMWARE
    assert "PIN_LED_DATA" in FIRMWARE
    assert "PIN_VIBRATION" in FIRMWARE
    assert "LED_COUNT" in FIRMWARE
