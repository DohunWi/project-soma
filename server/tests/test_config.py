"""Runtime profile selection tests."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import create_app  # noqa: E402
from server.config import (  # noqa: E402
    DEMO_PROFILE,
    NORMAL_PROFILE,
    ProfileConfigError,
)


def selected_profile(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SOMA_MODE", raising=False)
    else:
        monkeypatch.setenv("SOMA_MODE", value)
    app, _socketio = create_app(testing=True)
    return app.extensions["runtime_profile"]


def test_missing_soma_mode_selects_demo(monkeypatch):
    assert selected_profile(monkeypatch, None) is DEMO_PROFILE


def test_demo_soma_mode_selects_demo(monkeypatch):
    assert selected_profile(monkeypatch, "demo") is DEMO_PROFILE


def test_normal_soma_mode_selects_normal(monkeypatch):
    assert selected_profile(monkeypatch, "normal") is NORMAL_PROFILE


@pytest.mark.parametrize("value", ["", "DEMO", "production", " normal "])
def test_invalid_soma_mode_raises_clear_error(monkeypatch, value):
    monkeypatch.setenv("SOMA_MODE", value)

    with pytest.raises(ProfileConfigError, match="Invalid SOMA_MODE"):
        create_app(testing=True)


def test_runtime_profiles_include_reserved_storage_intervals():
    assert DEMO_PROFILE.storage.db_snapshot_interval_sec == 5.0
    assert NORMAL_PROFILE.storage.db_snapshot_interval_sec == 30.0
