"""Read authenticated measurement history without touching realtime state."""
import os
from datetime import datetime, timedelta, timezone


DEFAULT_HISTORY_WINDOW_SEC = 5 * 60
MAX_HISTORY_WINDOW_SEC = 60 * 60


class HistoryRequestError(ValueError):
    """The requested history period is missing or invalid."""


class HistoryUnavailable(RuntimeError):
    """The history database could not serve the request."""


def resolve_history_period(start_value, end_value, *, now=None):
    """Return a validated UTC period, defaulting an omitted pair to five minutes."""
    if start_value is None and end_value is None:
        end = now or datetime.now(timezone.utc)
        if end.tzinfo is None:
            raise HistoryRequestError("서버 기준 시각에는 timezone이 필요합니다.")
        end = end.astimezone(timezone.utc)
        return end - timedelta(seconds=DEFAULT_HISTORY_WINDOW_SEC), end

    if start_value is None or end_value is None:
        raise HistoryRequestError("start와 end는 함께 전달해야 합니다.")

    start = _parse_iso8601(start_value, "start")
    end = _parse_iso8601(end_value, "end")
    if start >= end:
        raise HistoryRequestError("start는 end보다 이전이어야 합니다.")
    if (end - start).total_seconds() > MAX_HISTORY_WINDOW_SEC:
        raise HistoryRequestError("조회 범위는 최대 60분입니다.")
    return start, end


def utc_iso8601(value):
    """Serialize a timezone-aware datetime as an explicit UTC ISO 8601 value."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso8601(value, field_name):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise HistoryRequestError(
            f"{field_name}는 timezone이 포함된 ISO 8601이어야 합니다."
        ) from error
    if parsed.tzinfo is None:
        raise HistoryRequestError(
            f"{field_name}는 timezone이 포함된 ISO 8601이어야 합니다."
        )
    return parsed.astimezone(timezone.utc)


HISTORY_SQL = """
WITH baseline AS (
    SELECT id, measured_at, state, score, confidence, reasons, balance,
           static_hold_sec, low_blink_sec, session_sec, chair_distance_mm,
           blink_rate, face_distance_cm, TRUE AS is_baseline
      FROM public.state_logs
     WHERE user_id = %s
       AND session_id = %s
       AND measured_at < %s
     ORDER BY measured_at DESC, id DESC
     LIMIT 1
), ranged AS (
    SELECT id, measured_at, state, score, confidence, reasons, balance,
           static_hold_sec, low_blink_sec, session_sec, chair_distance_mm,
           blink_rate, face_distance_cm, FALSE AS is_baseline
      FROM public.state_logs
     WHERE user_id = %s
       AND session_id = %s
       AND measured_at >= %s
       AND measured_at <= %s
)
SELECT measured_at, state, score, confidence, reasons, balance,
       static_hold_sec, low_blink_sec, session_sec, chair_distance_mm,
       blink_rate, face_distance_cm, is_baseline
  FROM (
        SELECT * FROM baseline
        UNION ALL
        SELECT * FROM ranged
       ) AS history
 ORDER BY measured_at ASC, id ASC
"""


class StateHistoryReader:
    """Fetch one authenticated session's measured-at-bounded snapshots."""

    def __init__(self, connection_factory=None):
        self._connection_factory = connection_factory

    def fetch(self, user_id, session_id, start, end):
        """Read a period in one DB statement and return API-ready snapshots."""
        connection = None
        cursor = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            cursor.execute(
                HISTORY_SQL,
                (
                    str(user_id),
                    str(session_id),
                    start,
                    str(user_id),
                    str(session_id),
                    start,
                    end,
                ),
            )
            rows = cursor.fetchall()
        except Exception as error:  # DB driver exceptions vary by failure mode.
            raise HistoryUnavailable("state history query failed") from error
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

        baseline = None
        states = []
        for row in rows:
            snapshot = _snapshot_from_row(row)
            if row[-1]:
                baseline = snapshot
            else:
                states.append(snapshot)
        return baseline, states

    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        if not all(os.getenv(key) for key in ("DB_HOST", "DB_USER", "DB_PASSWORD")):
            raise HistoryUnavailable("DB credentials are not configured")

        import psycopg2

        return psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            sslmode=os.getenv("DB_SSLMODE", "require"),
            connect_timeout=8,
        )


def _snapshot_from_row(row):
    metrics = {
        key: value
        for key, value in zip(
            (
                "balance",
                "static_hold_sec",
                "low_blink_sec",
                "session_sec",
                "chair_distance_mm",
                "blink_rate",
                "face_distance_cm",
            ),
            row[5:12],
        )
        if value is not None
    }
    snapshot = {
        "measured_at": utc_iso8601(row[0]),
        "state": row[1],
        "score": row[2],
        "confidence": row[3],
        "reasons": list(row[4] or []),
    }
    if metrics:
        snapshot["metrics"] = metrics
    return snapshot
