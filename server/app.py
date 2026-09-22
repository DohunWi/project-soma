"""Project Soma 실시간 백엔드.

수집 계층의 ``sensor_data`` 를 계약으로 검증하고, Chair 샘플을 메모리의
``FusionState`` 에 반영한 뒤 ``state`` 이벤트를 즉시 발행합니다.

실시간 경로는 Supabase와 독립적입니다. DB 관련 API를 호출할 때만 Supabase
클라이언트를 지연 생성하므로 DB 설정이나 인터넷 연결이 없어도 Chair → Front
경로는 계속 동작합니다.
"""
import logging
import os
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.state import FusionState, step  # noqa: E402
from server.config import DEMO_PROFILE, RuntimeProfile, load_runtime_profile  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

log = logging.getLogger("soma.server")

SENSOR_SCHEMA_PATH = ROOT / "docs" / "contracts" / "sensor_data.schema.json"
STATE_SCHEMA_PATH = ROOT / "docs" / "contracts" / "state.schema.json"


def _load_validator(path):
    """UTF-8 JSON schema를 읽어 Draft 2020-12 validator를 만듭니다."""
    import json

    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


SENSOR_VALIDATOR = _load_validator(SENSOR_SCHEMA_PATH)
STATE_VALIDATOR = _load_validator(STATE_SCHEMA_PATH)


class PayloadError(ValueError):
    """외부 payload 또는 fusion 출력이 계약을 위반했습니다."""


def _validation_message(validator, payload):
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


class ChairPipeline:
    """사용자·장치별 FusionState를 메모리에 유지하는 Chair 처리 계층."""

    def __init__(self, timing=DEMO_PROFILE.fusion):
        self._states = {}
        self._lock = threading.Lock()
        self._timing = timing

    def process(self, payload):
        """유효한 Chair payload를 state decision으로 변환합니다.

        Vision은 이번 단계의 처리 대상이 아닙니다. 유효한 Vision payload는
        오류로 취급하지 않고 ``None`` 을 반환해 독립 source 확장을 보존합니다.
        """
        message = _validation_message(SENSOR_VALIDATOR, payload)
        if message:
            raise PayloadError(f"sensor_data 계약 위반: {message}")
        if payload["source"] != "chair":
            return None

        chair = payload["chair"]
        sample = {
            "pressure": chair["pressure"],
            "ir": chair.get("ir", []),
            "user_name": payload["user_name"],
        }
        key = (payload["user_name"], payload.get("device_id", "smart_chair_01"))

        with self._lock:
            previous = self._states.get(key, FusionState())
            current, decision = step(
                previous,
                sample,
                payload["t"],
                timing=self._timing,
            )
            message = _validation_message(STATE_VALIDATOR, decision)
            if message:
                raise PayloadError(f"state 계약 위반: {message}")
            self._states[key] = current
        return decision


def _cors_origins():
    value = os.getenv("CORS_ALLOWED_ORIGINS", "*")
    if value == "*":
        return "*"
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _get_supabase_client():
    """DB API를 실제로 호출할 때만 선택적으로 Supabase에 연결합니다."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
    except ImportError:
        log.warning("supabase 패키지가 없어 DB API를 사용할 수 없습니다")
        return None
    try:
        return create_client(url, key)
    except Exception as error:  # Supabase 장애는 실시간 경로로 전파하지 않습니다.
        log.warning("Supabase 클라이언트 생성 실패: %s", error)
        return None


def create_app(*, testing=False, runtime_profile: RuntimeProfile | None = None):
    profile = runtime_profile or load_runtime_profile()
    app = Flask(__name__)
    app.config["TESTING"] = testing
    CORS(app, origins=_cors_origins())
    socketio = SocketIO(
        app,
        cors_allowed_origins=_cors_origins(),
        async_mode="threading",
        logger=False,
        engineio_logger=False,
    )
    pipeline = ChairPipeline(profile.fusion)
    app.extensions["chair_pipeline"] = pipeline
    app.extensions["runtime_profile"] = profile

    def token_required(function):
        @wraps(function)
        def decorated(*args, **kwargs):
            token = None
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ", 1)[1]
            if not token:
                return jsonify({"message": "토큰이 없습니다."}), 401

            client = _get_supabase_client()
            if client is None:
                return jsonify({"message": "Supabase가 설정되지 않았습니다."}), 503
            try:
                user = client.auth.get_user(token)
                user_id = user.user.id
            except Exception as error:
                return jsonify({"message": f"토큰 검증 실패: {error}"}), 401
            return function(user_id, *args, **kwargs)

        return decorated

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "realtime": True})

    @app.post("/api/measurement/start")
    @token_required
    def start_measurement(user_id):
        return jsonify({"status": "success", "message": "측정 시작", "user_id": user_id})

    @app.post("/api/measurement/stop")
    def stop_measurement():
        return jsonify({"status": "success", "message": "측정 종료"})

    @app.get("/api/report/weekly")
    @token_required
    def get_weekly_report(user_id):
        client = _get_supabase_client()
        if client is None:
            return jsonify({"status": "error", "message": "Supabase unavailable"}), 503
        try:
            response = (
                client.table("posture_stats_5min")
                .select("*")
                .eq("user_id", user_id)
                .order("target_time", desc=False)
                .execute()
            )
            return jsonify({"status": "success", "data": response.data})
        except Exception as error:
            return jsonify({"status": "error", "message": str(error)}), 503

    @socketio.on("sensor_data")
    def handle_sensor_data(payload):
        try:
            decision = pipeline.process(payload)
        except PayloadError as error:
            log.warning("payload 형식 오류, 이 샘플은 버립니다: %s", error)
            return
        except (KeyError, TypeError, ValueError) as error:
            log.warning("sensor_data 처리 오류, 이 샘플은 버립니다: %s", error)
            return

        if decision is None:
            log.debug("이번 단계에서 처리하지 않는 source입니다: %s", payload.get("source"))
            return

        # DB보다 Front가 먼저입니다. 이 경로에는 Supabase 호출이 없습니다.
        socketio.emit("state", decision)

    return app, socketio


app, socketio = create_app()


if __name__ == "__main__":
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "5000"))
    print(f"Project Soma backend: http://{host}:{port}")
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
