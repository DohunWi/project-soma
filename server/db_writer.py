"""
server/db_writer.py
───────────────────
DB 쓰기 전용 스레드.

**실제 Supabase 스키마에 맞춰 씁니다.** 새 테이블을 만들지 않습니다 —
DB 담당이 이미 sensor_logs / fatigue_logs / feedback_logs 로 나눠 놓았습니다.

    sensor_logs    원시 센서 + 간이 판정 (balance_status, posture_status)
    fatigue_logs   상태와 점수 (status enum, fatigue_score)
    feedback_logs  개입 기록과 수용 여부 (method enum, is_break_taken)

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
from datetime import datetime, timedelta, timezone

log = logging.getLogger("db")

_QUEUE_MAX = 2000
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
    def __init__(self, sensor_table=None):
        # 시연용 테이블을 따로 쓰고 싶으면 .env 의 DB_SENSOR_TABLE 로 바꿉니다
        self.sensor_table = sensor_table or os.getenv("DB_SENSOR_TABLE", "sensor_logs")
        self.q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0
        self.written = 0
        self._stop = threading.Event()
        self._conn = None
        self._t = threading.Thread(target=self._run, name="db-writer", daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)

    # ── 호출부 (이벤트 루프) ─────────────────────────────────────────────
    def put_sample(self, payload, decision):
        self._put(("sample", payload, decision))

    def put_state(self, decision):
        self._put(("state", decision))

    def put_feedback(self, cmd, accepted=None):
        self._put(("feedback", cmd, accepted))

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("DB 큐 포화 — %d건 버림", self.dropped)

    # ── 워커 스레드 ──────────────────────────────────────────────────────
    def _connect(self):
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
        while not self._stop.is_set():
            item = self.q.get()
            if item is None:
                break
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = self._connect()
                if self._conn is None:
                    continue
                self._write(item)
                self.written += 1
            except Exception as e:                     # noqa: BLE001
                # 조용히 삼키지 않습니다. 이게 안 보이면 적재 0건인 채로 시연이 끝납니다.
                log.warning("DB 쓰기 실패 (%s): %s", item[0], e)
                try:
                    self._conn.close()
                except Exception:                      # noqa: BLE001
                    pass
                self._conn = None
        if self._conn:
            self._conn.close()

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

        self._conn.commit()
        cur.close()
