"""Choose and map Fusion decisions for asynchronous state_logs storage."""
import threading
import uuid
from datetime import datetime, timezone


class StatePersistence:
    """Track the last enqueued snapshot independently for each logical stream."""

    def __init__(self, writer, snapshot_interval_sec, uuid_factory=uuid.uuid4):
        self.writer = writer
        self.snapshot_interval_sec = snapshot_interval_sec
        self._uuid_factory = uuid_factory
        self._last_snapshots = {}
        self._lock = threading.Lock()

    def handle(self, payload, decision):
        """Enqueue a state change or due periodic snapshot without DB I/O."""
        stream_key = (
            payload["user_name"],
            payload.get("device_id", "smart_chair_01"),
        )
        measured_t = float(decision["t"])

        with self._lock:
            previous = self._last_snapshots.get(stream_key)
            if previous is None or previous["state"] != decision["state"]:
                trigger = "state_change"
            elif measured_t - previous["t"] >= self.snapshot_interval_sec:
                trigger = "periodic"
            else:
                return None

            row = self._build_row(payload, decision, trigger)
            if not self.writer.put_state_log(row):
                return None

            self._last_snapshots[stream_key] = {
                "state": decision["state"],
                "t": measured_t,
            }
            return row

    def _build_row(self, payload, decision, trigger):
        metrics = decision.get("metrics") or {}
        chair = payload.get("chair") or {}
        ir = chair.get("ir") or []
        chair_distance = ir[0] if ir and ir[0] >= 0 else None

        return {
            "event_id": str(self._uuid_factory()),
            "user_id": None,
            "user_name": decision["user_name"],
            "device_id": payload.get("device_id", "smart_chair_01"),
            "measured_at": datetime.fromtimestamp(
                float(decision["t"]),
                timezone.utc,
            ),
            "trigger": trigger,
            "state": decision["state"],
            "score": decision["score"],
            "confidence": decision["confidence"],
            "reasons": list(decision.get("reasons") or []),
            "balance": metrics.get("balance"),
            "static_hold_sec": metrics.get("static_hold_sec"),
            "low_blink_sec": metrics.get("low_blink_sec"),
            "session_sec": metrics.get("session_sec"),
            "chair_distance_mm": chair_distance,
            "blink_rate": metrics.get("blink_rate"),
            "face_distance_cm": metrics.get("face_distance_cm"),
        }
