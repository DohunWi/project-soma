"""
server/db_writer.py
───────────────────
DB 쓰기 전용 스레드.

**실제 Supabase 스키마에 맞춰 씁니다.** migration은 실행하지 않습니다.
기존 legacy 경로와 신규 state_logs snapshot 경로를 함께 지원합니다.

    sensor_logs    원시 센서 + 간이 판정 (balance_status, posture_status)
    fatigue_logs   상태와 점수 (status enum, fatigue_score)
    feedback_logs  개입 기록과 수용 여부 (method enum, is_break_taken)
    state_logs     Fusion decision snapshot

**이벤트 루프에서 DB 를 직접 만지지 않습니다.**
psycopg2 는 C 확장이라 eventlet 이 monkey_patch 하지 못합니다.
커넥션 풀도 없이 원격 Supabase 로 매번 TCP+TLS 를 새로 맺으면
그때마다 소켓 이벤트 루프 전체가 왕복 시간만큼 멈춥니다.
큐에 넣고 별도 스레드가 처리하며, 큐가 넘치면 버립니다 —
실시간 표시가 DB 때문에 밀리는 것보다 낫습니다.

**시각 규약**
DB 의 time 컬럼은 timestamp without time zone 이고 기본값이 KST 로컬입니다.
그 관례를 따르되, **계약의 t(측정 시각)를 measured_at 에 함께 넣습니다.**
의자와 웹캠은 서로 다른 프로세스라 각자의 t 로 정렬해야 하는데,
DB 수신 시각(now())으로 덮이면 정렬이 여기서 깨집니다.
"""
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("db")

_QUEUE_MAX = 2000
_STATE_LOG_MAX_ATTEMPTS = 3
_STATE_LOG_BACKOFF_BASE_SEC = 0.5
KST = timezone(timedelta(hours=9))

# 서버 판정 → DB enum. DB 가 정본입니다.
STATUS_ENUM = {"NORMAL": "NORMAL", "CAUTION": "CAUTION",
               "DANGER": "DANGER", "ABSENT": "ABSENT"}
METHOD_ENUM = {"chair_vibration": "CHAIR_VIBRATION",
               "ambient_led":     "AMBIENT_LIGHT",
               "web_popup":       "UI_ALERT"}


def kst_naive(epoch):
    """epoch → KST 로컬 naive datetime. DB 의 time 관례에 맞춥니다."""
    return datetime.fromtimestamp(epoch, KST).replace(tzinfo=None)


class DBWriter:
    def __init__(
        self,
        sensor_table=None,
        *,
        queue_max=_QUEUE_MAX,
        state_log_max_attempts=_STATE_LOG_MAX_ATTEMPTS,
        state_log_backoff_base_sec=_STATE_LOG_BACKOFF_BASE_SEC,
        connection_factory=None,
        sleep=time.sleep,
    ):
        # 시연용 테이블을 따로 쓰고 싶으면 .env 의 DB_SENSOR_TABLE 로 바꿉니다
        self.sensor_table = sensor_table or os.getenv("DB_SENSOR_TABLE", "sensor_logs")
        self.q: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self.state_log_max_attempts = state_log_max_attempts
        self.state_log_backoff_base_sec = state_log_backoff_base_sec
        self._connection_factory = connection_factory
        self._sleep = sleep
        self.dropped = 0
        self.written = 0
        self.failed = 0
        self._stop = threading.Event()
        self._conn = None
        self._t = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._started = False

    @staticmethod
    def is_configured():
        """Return whether the direct PostgreSQL credentials are complete."""
        return all(os.getenv(key) for key in ("DB_HOST", "DB_USER", "DB_PASSWORD"))

    def start(self):
        if self._started:
            return
        self._started = True
        self._t.start()

    def stop(self, timeout=1.0):
        self._stop.set()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        if self._started:
            self._t.join(timeout=timeout)

    # ── 호출부 (이벤트 루프) ─────────────────────────────────────────────
    def put_sample(self, payload, decision):
        self._put(("sample", payload, decision))

    def put_state(self, decision):
        self._put(("state", decision))

    def put_feedback(self, cmd, accepted=None):
        return self._put(("feedback", cmd, accepted))

    def put_state_log(self, row):
        """Queue one immutable logical snapshot for state_logs."""
        return self._put(("state_log", row))

    def _put(self, item):
        if self._stop.is_set():
            return False
        try:
            self.q.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("DB 큐 포화 — %d건 버림", self.dropped)
            return False

    # ── 워커 스레드 ──────────────────────────────────────────────────────
    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        import psycopg2
        host = os.getenv("DB_HOST")
        if not host:
            log.warning("DB_HOST 가 비어 있습니다 — DB 적재를 건너뜁니다")
            return None
        return psycopg2.connect(
            host=host, port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
            sslmode=os.getenv("DB_SSLMODE", "require"), connect_timeout=8)

    def _run(self):
        while True:
            if self._stop.is_set() and self.q.empty():
                break
            try:
                item = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item is None:
                    break
                self._process_item(item)
            finally:
                self.q.task_done()
        self._close_connection()

    def _process_item(self, item):
        max_attempts = self.state_log_max_attempts if item[0] == "state_log" else 1
        for attempt in range(max_attempts):
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = self._connect()
                if self._conn is None:
                    raise ConnectionError("DB persistence is not configured")
                self._write(item)
                self.written += 1
                return
            except Exception as error:                 # noqa: BLE001
                log.warning(
                    "DB 쓰기 실패 (%s, attempt %d/%d): %s",
                    item[0],
                    attempt + 1,
                    max_attempts,
                    error,
                )
                self._close_connection()
                if attempt + 1 < max_attempts:
                    self._sleep(self.state_log_backoff_base_sec * (2 ** attempt))
        self.failed += 1

    def _close_connection(self):
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:                              # noqa: BLE001
            pass
        self._conn = None

    def _write(self, item):
        kind = item[0]
        cur = self._conn.cursor()

        if kind == "sample":
            _, payload, d = item
            m = d.get("metrics", {})
            # 구 컬럼(balance_status, posture_status)도 채웁니다.
            # 기존 대시보드·뷰가 이 컬럼을 보고 있을 수 있습니다.
            cur.execute(
                f'''INSERT INTO {self.sensor_table}
                    (user_name, raw_data, balance_status, posture_status,
                     "time", measured_at, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                (payload.get("user_name"), json.dumps(payload.get("chair") or
                                                      payload.get("vision") or {}),
                 m.get("balance"),
                 "Empty" if not m.get("seated") else
                 ("Leaning Forward" if "close_distance" in (d.get("reasons") or [])
                  else "Seated"),
                 kst_naive(d["t"]), kst_naive(payload["t"]), payload.get("source")))

        elif kind == "state":
            _, d = item
            cur.execute(
                '''INSERT INTO fatigue_logs
                   ("timestamp", status, fatigue_score, raw_data_summary)
                   VALUES (%s, %s::user_status, %s, %s)''',
                (kst_naive(d["t"]), STATUS_ENUM.get(d["state"], "NORMAL"),
                 float(d.get("score", 0)), json.dumps({
                     "user_name": d.get("user_name"),
                     "confidence": d.get("confidence"),
                     "reasons": d.get("reasons"),
                     "metrics": d.get("metrics"),
                 }, ensure_ascii=False)))

        elif kind == "feedback":
            _, cmd, accepted = item
            method = METHOD_ENUM.get(cmd.get("target"))
            if method is None:
                cur.close()
                return
            cur.execute(
                '''INSERT INTO feedback_logs
                   ("timestamp", method, is_break_taken)
                   VALUES (%s, %s::feedback_method, %s)''',
                (kst_naive(cmd["t"]), method, accepted))

        elif kind == "state_log":
            _, row = item
            cur.execute(
                '''INSERT INTO state_logs
                   (event_id, user_id, session_id, user_name, device_id, measured_at,
                    trigger, state, score, confidence, reasons, balance,
                    static_hold_sec, low_blink_sec, session_sec,
                    chair_distance_mm, blink_rate, face_distance_cm)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_id) DO NOTHING''',
                (
                    row["event_id"], row["user_id"], row["session_id"],
                    row["user_name"], row["device_id"], row["measured_at"],
                    row["trigger"], row["state"], row["score"],
                    row["confidence"], row["reasons"], row["balance"],
                    row["static_hold_sec"], row["low_blink_sec"],
                    row["session_sec"], row["chair_distance_mm"],
                    row["blink_rate"], row["face_distance_cm"],
                ),
            )

        self._conn.commit()
        cur.close()
