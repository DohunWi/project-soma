"""Read and summarize one authenticated state_logs measurement session."""
import os
from datetime import datetime

from server.state_history import utc_iso8601


REPORT_GAP_LIMIT_SEC = 60.0


class SessionReportUnavailable(RuntimeError):
    """The report database could not serve the request."""


class SessionReportNotFound(LookupError):
    """No snapshots belong to the authenticated user/session pair."""


SESSION_REPORT_SQL = """
SELECT measured_at, trigger, state, score, reasons, score_model_version
  FROM public.state_logs
 WHERE user_id = %s
   AND session_id = %s
 ORDER BY measured_at ASC, id ASC
"""


class SessionReportReader:
    """Fetch a complete session without changing the bounded History API."""

    def __init__(self, connection_factory=None):
        self._connection_factory = connection_factory

    def fetch(self, user_id, session_id):
        connection = cursor = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            cursor.execute(SESSION_REPORT_SQL, (str(user_id), str(session_id)))
            rows = cursor.fetchall()
        except Exception as error:
            raise SessionReportUnavailable("session report query failed") from error
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()
        if not rows:
            raise SessionReportNotFound("recorded session not found")
        return [{"measured_at": row[0], "trigger": row[1], "state": row[2],
                 "score": row[3], "reasons": list(row[4] or []),
                 "score_model_version": row[5]} for row in rows]

    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        if not all(os.getenv(key) for key in ("DB_HOST", "DB_USER", "DB_PASSWORD")):
            raise SessionReportUnavailable("DB credentials are not configured")
        import psycopg2
        return psycopg2.connect(
            host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "postgres"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), sslmode=os.getenv("DB_SSLMODE", "require"),
            connect_timeout=8,
        )


def build_session_report(user_id, session_id, snapshots, *, gap_limit_sec=REPORT_GAP_LIMIT_SEC):
    """Return a report from persisted snapshots, never inferred lifecycle events."""
    if gap_limit_sec <= 0:
        raise ValueError("gap_limit_sec must be positive")
    if not snapshots:
        raise SessionReportNotFound("recorded session not found")
    ordered = sorted(snapshots, key=lambda item: item["measured_at"])
    for snapshot in ordered:
        measured_at = snapshot.get("measured_at")
        if not isinstance(measured_at, datetime) or measured_at.tzinfo is None:
            raise ValueError("snapshot measured_at must be timezone-aware")

    states = ("NORMAL", "CAUTION", "DANGER", "ABSENT")
    state_seconds = {state: 0.0 for state in states}
    reason_seconds = {}
    trigger_counts = {"state_change": 0, "periodic": 0}
    score_weight = seated_seconds = absent_seconds = 0.0
    valid_intervals = long_gaps = nonpositive_intervals = 0
    for snapshot in ordered:
        trigger = snapshot.get("trigger")
        if trigger in trigger_counts:
            trigger_counts[trigger] += 1

    for current, following in zip(ordered, ordered[1:]):
        elapsed = (following["measured_at"] - current["measured_at"]).total_seconds()
        if elapsed <= 0:
            nonpositive_intervals += 1
            continue
        if elapsed > gap_limit_sec:
            long_gaps += 1
            continue
        state = current["state"]
        if state not in state_seconds:
            raise ValueError(f"unsupported state: {state!r}")
        valid_intervals += 1
        state_seconds[state] += elapsed
        if state == "ABSENT":
            absent_seconds += elapsed
            continue
        seated_seconds += elapsed
        score_weight += float(current["score"]) * elapsed
        for reason in current.get("reasons") or []:
            reason_seconds[reason] = reason_seconds.get(reason, 0.0) + elapsed

    first, last = ordered[0]["measured_at"], ordered[-1]["measured_at"]
    span_seconds = max((last - first).total_seconds(), 0.0)
    observed_seconds = seated_seconds + absent_seconds
    seated_scores = [int(item["score"]) for item in ordered if item["state"] != "ABSENT"]
    versions = sorted({str(item["score_model_version"]) for item in ordered
                       if item.get("score_model_version") is not None})
    major_reasons = [
        {"reason": reason, "seconds": _rounded(seconds)}
        for reason, seconds in sorted(reason_seconds.items(), key=lambda item: (-item[1], item[0]))
    ]
    timeseries, previous_at = [], None
    for item in ordered:
        measured_at = item["measured_at"]
        timeseries.append({
            "measured_at": utc_iso8601(measured_at), "state": item["state"],
            "score": int(item["score"]),
            "gap_before": previous_at is not None and
                          (measured_at - previous_at).total_seconds() > gap_limit_sec,
        })
        previous_at = measured_at

    return {
        "v": 1, "status": "success",
        "stream": {"user_id": str(user_id), "session_id": str(session_id)},
        "period": {"start": utc_iso8601(first), "end": utc_iso8601(last),
                   "basis": "snapshot_bounds", "lifecycle_exact": False},
        "summary": {
            "snapshot_span_sec": _rounded(span_seconds),
            "observed_coverage_sec": _rounded(observed_seconds),
            "seated_observed_sec": _rounded(seated_seconds),
            "absent_observed_sec": _rounded(absent_seconds),
            "unknown_gap_sec": _rounded(max(span_seconds - observed_seconds, 0.0)),
            "average_score": round(score_weight / seated_seconds, 2) if seated_seconds > 0 else None,
            "minimum_score": min(seated_scores) if seated_scores else None,
            "state_seconds": {state: _rounded(state_seconds[state]) for state in states},
            "major_reasons": major_reasons,
            "break_feedback": {"supported": False, "occurred": None,
                               "message": "현재 저장 데이터로 확인 불가"},
        },
        "quality": {
            "snapshot_count": len(ordered), "valid_interval_count": valid_intervals,
            "long_gap_count": long_gaps, "nonpositive_interval_count": nonpositive_intervals,
            "gap_limit_sec": float(gap_limit_sec), "single_snapshot": len(ordered) == 1,
            "score_model_versions": versions,
            "legacy_unversioned": any(item.get("score_model_version") is None for item in ordered),
            "trigger_counts": trigger_counts,
        },
        "timeseries": timeseries,
    }


def _rounded(value):
    return round(float(value), 3)
