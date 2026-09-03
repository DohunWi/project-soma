#!/usr/bin/env python3
"""
server/app.py
─────────────
Flask + Socket.IO 중계 서버.

    수집 → [검증 → 병합 → fusion → 판정] → DB / UI / 액추에이터

이전 구현에서 고친 것:
  - 판정 로직을 fusion/state.py 로 분리했습니다. 여기서는 호출만 합니다
  - DB 쓰기를 큐 + 스레드로 뺐습니다 (server/db_writer.py 주석 참조)
  - DB 자격증명을 .env 로 옮겼습니다. 코드에 평문으로 있었습니다
  - cors_allowed_origins='*' 와 인증 없음을 고쳤습니다.
    공개 저장소 + 인증 없는 소켓 = 누구나 DB 에 쓸 수 있는 상태였습니다
  - payload 를 스키마로 검증합니다. 이전에는 dict 를 직접 인덱싱하고
    실패하면 except 로 삼켜서, 무엇이 왜 실패했는지 알 수 없었습니다

실행:
    python server/app.py
"""
import eventlet
eventlet.monkey_patch()

import json
import logging
import os
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request
import socketio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fusion"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from state import FusionState, step          # fusion/state.py
from db_writer import DBWriter               # server/db_writer.py

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
log = logging.getLogger("server")

# ── 설정 ─────────────────────────────────────────────────────────────
PORT         = int(os.getenv("SERVER_PORT", 5000))
HOST         = os.getenv("SERVER_HOST", "0.0.0.0")
AUTH_TOKEN   = os.getenv("SOCKET_AUTH_TOKEN") or None
CORS         = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
MERGE_WINDOW = 2.0    # 초. 이보다 오래된 다른 소스 값은 쓰지 않습니다

if not CORS:
    log.warning("CORS_ALLOWED_ORIGINS 가 비어 있어 '*' 로 엽니다 — 개발용으로만 쓰세요")
    CORS = "*"
if not AUTH_TOKEN:
    log.warning("SOCKET_AUTH_TOKEN 이 비어 있어 인증 없이 받습니다 — 개발용으로만 쓰세요")

sio = socketio.Server(cors_allowed_origins=CORS, async_mode="eventlet")
flask_app = Flask(__name__)
app = socketio.WSGIApp(sio, flask_app)

db = DBWriter()

# ── 스키마 검증 ──────────────────────────────────────────────────────
_validator = None
try:
    from jsonschema import Draft202012Validator
    with open(ROOT / "docs/contracts/sensor_data.schema.json", encoding="utf-8") as f:
        _validator = Draft202012Validator(json.load(f))
except Exception as e:                                   # noqa: BLE001
    log.warning("스키마 검증 비활성 (%s)", e)


def validate(payload):
    """실패 사유를 반환합니다. 조용히 삼키지 않습니다."""
    if _validator is None:
        return None
    errs = list(_validator.iter_errors(payload))
    if not errs:
        return None
    return "; ".join(f"{list(e.path)}: {e.message}" for e in errs[:3])


# ── 소스별 최신값 + fusion 상태 (사용자별) ───────────────────────────
class Session:
    def __init__(self):
        self.fusion = FusionState()
        self.chair = None          # (t, dict)
        self.vision = None
        self.last_emit = 0.0

sessions: dict = {}


def merged(sess, now):
    """chair 와 vision 의 최신값을 t 로 합칩니다. 오래된 쪽은 버립니다."""
    out = {}
    for slot in (sess.chair, sess.vision):
        if slot and now - slot[0] <= MERGE_WINDOW:
            out.update(slot[1])
    return out


# ── Socket.IO ────────────────────────────────────────────────────────
@sio.event
def connect(sid, environ, auth):
    if AUTH_TOKEN:
        token = (auth or {}).get("token")
        if token != AUTH_TOKEN:
            log.warning("인증 실패로 연결 거부: %s", sid)
            raise socketio.exceptions.ConnectionRefusedError("unauthorized")
    log.info("연결 %s", sid)


@sio.event
def disconnect(sid):
    log.info("해제 %s", sid)


@sio.on("sensor_data")
def on_sensor_data(sid, payload):
    if isinstance(payload, str):
        payload = json.loads(payload)

    reason = validate(payload)
    if reason:
        log.warning("스키마 위반, 버림: %s", reason)
        return

    user = payload["user_name"]
    src  = payload["source"]
    t    = payload["t"]
    sess = sessions.setdefault(user, Session())

    if src == "chair":
        c = payload.get("chair", {})
        sess.chair = (t, {"pressure": c.get("pressure"), "ir": c.get("ir")})
    else:
        v = payload.get("vision", {})
        sess.vision = (t, {
            "blink_rate":       v.get("blink_rate"),
            "face_distance_cm": v.get("face_distance_cm"),
            "face_detected":    v.get("face_detected"),
        })
        if v.get("blink"):
            db.put_blink(user, t)

    now = max(t, time.time())
    sample = merged(sess, now)
    sample["user_name"] = user

    sess.fusion, decision = step(sess.fusion, sample, now)

    sio.emit("state", decision)

    # DB 는 1초에 한 번만. 원본 해상도가 필요하면 blink_events 를 보세요
    if now - sess.last_emit >= 1.0:
        sess.last_emit = now
        db.put_sample(user, payload, decision)


@sio.on("feedback")
def on_feedback(sid, payload):
    """액추에이터로 그대로 중계합니다. 정책은 feedback/policy 가 정합니다."""
    sio.emit("feedback", payload, skip_sid=sid)


# ── HTTP ─────────────────────────────────────────────────────────────
@flask_app.route("/api/report", methods=["POST"])
def receive_report():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "JSON 본문이 없습니다"}), 400
    log.info("보고서 수신: %s %s", data.get("user_name"), data.get("kind"))
    sio.emit("report", data)               # 프론트로 즉시 푸시
    return jsonify({"status": "ok"}), 200


@flask_app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "sessions": list(sessions),
        "db_queue": db.q.qsize(),
        "db_dropped": db.dropped,
    })


if __name__ == "__main__":
    db.start()
    log.info("서버 시작 http://%s:%d  (auth=%s, cors=%s)",
             HOST, PORT, "on" if AUTH_TOKEN else "off", CORS)
    try:
        eventlet.wsgi.server(eventlet.listen((HOST, PORT)), app, log_output=False)
    finally:
        db.stop()
