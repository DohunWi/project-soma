"""Static safety checks for firmware when Arduino CLI is unavailable."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = (
    ROOT
    / "feedback"
    / "nano"
    / "firmware"
    / "feedback_nano"
    / "feedback_nano.ino"
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


def test_firmware_matches_validated_feedback_nano_wiring():
    assert "const uint8_t PIN_LED_DATA = 13;" in FIRMWARE
    assert "const uint8_t PIN_VIBRATION = 9;" in FIRMWARE
    assert "const uint16_t LED_COUNT = 6;" in FIRMWARE
    assert "TODO(hardware E2E)" not in FIRMWARE
