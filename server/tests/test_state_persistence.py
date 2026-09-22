"""State snapshot selection and state_logs row mapping tests."""
import sys
from datetime import timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.config import DEMO_PROFILE, NORMAL_PROFILE  # noqa: E402
from server.state_persistence import StatePersistence  # noqa: E402


class CollectingWriter:
    def __init__(self, accepts=True):
        self.accepts = accepts
        self.rows = []

    def put_state_log(self, row):
        if not self.accepts:
            return False
        self.rows.append(row)
        return True


def payload(*, user="guest", device="chair-1", ir=250):
    chair = {"pressure": [850, 850, 850, 850]}
    if ir is not None:
        chair["ir"] = [ir]
    return {
        "v": 1,
        "t": 1000.0,
        "source": "chair",
        "user_name": user,
        "device_id": device,
        "chair": chair,
    }


def decision(*, t=1000.0, state="NORMAL", vision=False):
    metrics = {
        "balance": "CENTER",
        "static_hold_sec": 3.0,
        "low_blink_sec": 0.0,
        "session_sec": 8.0,
    }
    if vision:
        metrics.update({"blink_rate": 12.5, "face_distance_cm": 55.0})
    return {
        "v": 1,
        "t": t,
        "user_name": "guest",
        "state": state,
        "score": 90,
        "confidence": 0.45,
        "reasons": [],
        "metrics": metrics,
    }


def persistence(writer, profile=DEMO_PROFILE):
    return StatePersistence(
        writer,
        profile.storage.db_snapshot_interval_sec,
    )


def test_first_decision_is_state_change_snapshot():
    writer = CollectingWriter()
    row = persistence(writer).handle(payload(), decision())

    assert row["trigger"] == "state_change"
    assert writer.rows == [row]


def test_same_state_before_interval_is_not_stored():
    writer = CollectingWriter()
    store = persistence(writer)
    store.handle(payload(), decision(t=1000.0))

    assert store.handle(payload(), decision(t=1004.999)) is None
    assert len(writer.rows) == 1


@pytest.mark.parametrize(
    ("profile", "before", "due"),
    [
        (DEMO_PROFILE, 4.999, 5.0),
        (NORMAL_PROFILE, 29.999, 30.0),
    ],
)
def test_profile_interval_creates_periodic_snapshot(profile, before, due):
    writer = CollectingWriter()
    store = persistence(writer, profile)
    store.handle(payload(), decision(t=1000.0))

    assert store.handle(payload(), decision(t=1000.0 + before)) is None
    row = store.handle(payload(), decision(t=1000.0 + due))

    assert row["trigger"] == "periodic"


def test_state_change_is_stored_before_interval():
    writer = CollectingWriter()
    store = persistence(writer)
    store.handle(payload(), decision(t=1000.0, state="NORMAL"))

    row = store.handle(payload(), decision(t=1001.0, state="CAUTION"))

    assert row["trigger"] == "state_change"
    assert row["state"] == "CAUTION"


def test_streams_are_independent_by_user_and_device():
    writer = CollectingWriter()
    store = persistence(writer)
    store.handle(payload(user="a", device="one"), decision(t=1000.0))

    other = store.handle(
        payload(user="b", device="two"),
        {**decision(t=1001.0), "user_name": "b"},
    )

    assert other["trigger"] == "state_change"
    assert len(writer.rows) == 2


def test_each_snapshot_gets_a_new_event_id():
    writer = CollectingWriter()
    store = persistence(writer)
    first = store.handle(payload(), decision(t=1000.0))
    second = store.handle(payload(), decision(t=1005.0))

    assert first["event_id"] != second["event_id"]


@pytest.mark.parametrize(
    ("ir", "expected"),
    [(-1, None), (250, 250), (0, 0), (None, None)],
)
def test_chair_distance_mapping(ir, expected):
    row = persistence(CollectingWriter()).handle(payload(ir=ir), decision())
    assert row["chair_distance_mm"] == expected


def test_chair_only_row_has_null_vision_fields_and_user_id():
    row = persistence(CollectingWriter()).handle(payload(), decision())

    assert row["user_id"] is None
    assert row["blink_rate"] is None
    assert row["face_distance_cm"] is None


def test_vision_metrics_are_copied_when_present():
    row = persistence(CollectingWriter()).handle(payload(), decision(vision=True))

    assert row["blink_rate"] == 12.5
    assert row["face_distance_cm"] == 55.0


def test_measured_at_is_timezone_aware_utc():
    row = persistence(CollectingWriter()).handle(payload(), decision(t=1000.25))

    assert row["measured_at"].tzinfo is timezone.utc
    assert row["measured_at"].timestamp() == 1000.25


def test_rejected_enqueue_does_not_advance_snapshot_checkpoint():
    writer = CollectingWriter(accepts=False)
    store = persistence(writer)
    assert store.handle(payload(), decision(t=1000.0)) is None

    writer.accepts = True
    row = store.handle(payload(), decision(t=1001.0))

    assert row["trigger"] == "state_change"
