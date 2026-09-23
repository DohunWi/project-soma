"""Thread-safe in-memory lifecycle for the single physical Soma chair."""
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone


class MeasurementInUse(RuntimeError):
    """The single chair already belongs to another active user."""


class NoMeasurementSession(RuntimeError):
    """The authenticated user has no current or recently stopped session."""


@dataclass(frozen=True)
class MeasurementSession:
    user_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    started_at: datetime
    ended_at: datetime | None = None

    def as_dict(self):
        data = {
            "session_id": str(self.session_id),
            "user_id": str(self.user_id),
            "status": self.status,
            "started_at": _utc_iso8601(self.started_at),
        }
        if self.ended_at is not None:
            data["ended_at"] = _utc_iso8601(self.ended_at)
        return data


class MeasurementSessionRegistry:
    """Allow exactly one ACTIVE session because Project Soma has one chair."""

    def __init__(self, *, uuid_factory=uuid.uuid4, now=None):
        self._uuid_factory = uuid_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._active = None
        self._last_stopped = {}
        self._lock = threading.RLock()

    def start(self, user_id):
        with self._lock:
            if self._active is not None:
                if self._active.user_id == user_id:
                    return self._active, False
                raise MeasurementInUse("measurement is already active")
            session = MeasurementSession(
                user_id=user_id,
                session_id=self._uuid_factory(),
                status="ACTIVE",
                started_at=_aware_utc(self._now()),
            )
            self._active = session
            return session, True

    def stop(self, user_id):
        with self._lock:
            if self._active is not None and self._active.user_id == user_id:
                stopped = replace(
                    self._active,
                    status="STOPPED",
                    ended_at=_aware_utc(self._now()),
                )
                self._active = None
                self._last_stopped[user_id] = stopped
                return stopped, False
            if self._active is None and user_id in self._last_stopped:
                return self._last_stopped[user_id], True
            raise NoMeasurementSession("no active measurement session")

    def active(self):
        with self._lock:
            return self._active

    def is_active(self, session):
        with self._lock:
            return self._active == session


def _aware_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("measurement clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_iso8601(value):
    return _aware_utc(value).isoformat().replace("+00:00", "Z")
