"""
server/db_writer.py
───────────────────
DB 쓰기 전용 스레드.

**이벤트 루프에서 DB 를 직접 만지지 않습니다.**
psycopg2 는 C 확장이라 eventlet 이 monkey_patch 하지 못합니다.
커넥션 풀도 없이 5초마다 원격 Supabase 로 TCP+TLS 핸드셰이크를 새로 맺으면,
그때마다 소켓 이벤트 루프 전체가 왕복 시간만큼 멈춥니다.
발표장 네트워크가 흔들리면 대시보드가 얼어붙습니다.

큐에 넣고 별도 스레드가 처리합니다. 큐가 넘치면 **버립니다** —
실시간 표시가 DB 때문에 밀리는 것보다 낫습니다.
"""
import json
import logging
import os
import queue
import threading

log = logging.getLogger("db")

_QUEUE_MAX = 2000


class DBWriter:
    def __init__(self):
        self.q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0
        self._stop = threading.Event()
        self._conn = None
        self._t = threading.Thread(target=self._run, name="db-writer", daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)

    # ── 호출부 (이벤트 루프) ─────────────────────────────────────────────
    def put_sample(self, user_name, payload, decision):
        self._put(("sample", user_name, payload, decision))

    def put_blink(self, user_name, t):
        """깜빡임은 초 단위 사건이라 별도 테이블에 낱개로 넣습니다."""
        self._put(("blink", user_name, t))

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
        cfg = dict(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            connect_timeout=5,
        )
        if not cfg["host"]:
            log.warning("DB_HOST 가 비어 있습니다 — DB 적재를 건너뜁니다")
            return None
        return psycopg2.connect(**cfg)

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
            except Exception as e:                     # noqa: BLE001
                log.warning("DB 쓰기 실패 (%s): %s", item[0], e)
                try:
                    self._conn.close()
                except Exception:                      # noqa: BLE001
                    pass
                self._conn = None                      # 다음 항목에서 재연결

        if self._conn:
            self._conn.close()

    def _write(self, item):
        kind = item[0]
        cur = self._conn.cursor()
        if kind == "sample":
            _, user_name, payload, d = item
            m = d.get("metrics", {})
            cur.execute(
                """INSERT INTO sensor_logs
                   (t, user_name, raw_data, state, score, balance,
                    blink_rate, face_distance_cm, static_hold_sec)
                   VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)""",
                (d["t"], user_name, json.dumps(payload), d["state"], d.get("score"),
                 m.get("balance"), m.get("blink_rate"), m.get("face_distance_cm"),
                 m.get("static_hold_sec")),
            )
        elif kind == "blink":
            _, user_name, t = item
            cur.execute(
                "INSERT INTO blink_events (t, user_name) VALUES (to_timestamp(%s), %s)",
                (t, user_name),
            )
        self._conn.commit()
        cur.close()
