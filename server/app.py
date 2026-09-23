"""Project Soma 실시간 백엔드.

수집 계층의 ``sensor_data`` 를 항상 계약으로 검증합니다. 인증 사용자의 measurement가
ACTIVE일 때만 Chair 샘플을 새 ``FusionState`` 에 반영하고 사용자 room에 ``state``를
발행한 뒤 비동기 persistence를 시도합니다.

실시간 경로는 Supabase와 독립적입니다. snapshot 저장은 비동기 DBWriter로 넘기고
History DB 조회는 해당 HTTP 요청에서만 수행하므로, DB 설정이나 인터넷 연결이 없어도
Chair → Front 경로는 계속 동작합니다.
"""
import hmac
import logging
import os
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.state import FusionState, step  # noqa: E402
from server.auth import (  # noqa: E402
    AuthenticationError,
    AuthenticationUnavailable,
    SupabaseAuthVerifier,
    bearer_token,
)
from server.config import DEMO_PROFILE, RuntimeProfile, load_runtime_profile  # noqa: E402
from server.db_writer import DBWriter  # noqa: E402
from server.measurement_sessions import (  # noqa: E402
    MeasurementInUse,
    MeasurementSessionRegistry,
    NoMeasurementSession,
)
from server.state_history import (  # noqa: E402
    HistoryRequestError,
    HistoryUnavailable,
    StateHistoryReader,
    resolve_history_period,
    utc_iso8601,
)
from server.state_persistence import StatePersistence  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

log = logging.getLogger("soma.server")

SENSOR_SCHEMA_PATH = ROOT / "docs" / "contracts" / "sensor_data.schema.json"
STATE_SCHEMA_PATH = ROOT / "docs" / "contracts" / "state.schema.json"
STATE_HISTORY_SCHEMA_PATH = ROOT / "docs" / "contracts" / "state_history.schema.json"
_AUTO_PERSISTENCE = object()
_AUTO_HISTORY_READER = object()


def _load_validator(path):
    """UTF-8 JSON schema를 읽어 Draft 2020-12 validator를 만듭니다."""
    import json

    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


SENSOR_VALIDATOR = _load_validator(SENSOR_SCHEMA_PATH)
STATE_VALIDATOR = _load_validator(STATE_SCHEMA_PATH)
STATE_HISTORY_VALIDATOR = _load_validator(STATE_HISTORY_SCHEMA_PATH)


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
    """단일 실제 Chair의 FusionState를 메모리에 유지하는 처리 계층."""

    def __init__(self, timing=DEMO_PROFILE.fusion):
        self._state = FusionState()
        self._lock = threading.Lock()
        self._timing = timing

    def validate(self, payload):
        """Validate sensor_data without advancing Fusion state."""
        message = _validation_message(SENSOR_VALIDATOR, payload)
        if message:
            raise PayloadError(f"sensor_data 계약 위반: {message}")

    def process(self, payload):
        """Validate and convert one Chair payload to a state decision.

        Vision은 이번 단계의 처리 대상이 아닙니다. 유효한 Vision payload는
        오류로 취급하지 않고 ``None`` 을 반환해 독립 source 확장을 보존합니다.
        """
        self.validate(payload)
        return self.process_validated(payload)

    def process_validated(self, payload):
        """Advance Fusion for one payload already checked against the contract."""
        if payload["source"] != "chair":
            return None

        chair = payload["chair"]
        sample = {
            "pressure": chair["pressure"],
            "ir": chair.get("ir", []),
            "user_name": payload["user_name"],
        }

        with self._lock:
            current, decision = step(
                self._state,
                sample,
                payload["t"],
                timing=self._timing,
            )
            message = _validation_message(STATE_VALIDATOR, decision)
            if message:
                raise PayloadError(f"state 계약 위반: {message}")
            self._state = current
        return decision

    def reset(self):
        """Start a measurement session with no prior accumulated Fusion state."""
        with self._lock:
            self._state = FusionState()


def _cors_origins():
    value = os.getenv("CORS_ALLOWED_ORIGINS", "*")
    if value == "*":
        return "*"
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _get_supabase_client():
    """DB API를 실제로 호출할 때만 선택적으로 Supabase에 연결합니다."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_KEY")
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


def create_app(
    *,
    testing=False,
    runtime_profile: RuntimeProfile | None = None,
    state_persistence=_AUTO_PERSISTENCE,
    history_reader=_AUTO_HISTORY_READER,
    history_now=None,
    auth_verifier=None,
    session_registry=None,
    sensor_auth_token=None,
):
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
    verifier = auth_verifier or SupabaseAuthVerifier()
    sessions = session_registry or MeasurementSessionRegistry()
    measurement_lock = threading.RLock()
    socket_identities = {}
    socket_identity_lock = threading.Lock()
    if sensor_auth_token is None:
        sensor_auth_token = os.getenv("SOCKET_AUTH_TOKEN")
    db_writer = None
    persistence = state_persistence
    if persistence is _AUTO_PERSISTENCE:
        persistence = None
        if not testing and DBWriter.is_configured():
            db_writer = DBWriter()
            db_writer.start()
            persistence = StatePersistence(
                db_writer,
                profile.storage.db_snapshot_interval_sec,
            )
        elif not testing:
            log.info("DB credentials가 없어 state_logs persistence를 비활성화합니다")

    app.extensions["chair_pipeline"] = pipeline
    app.extensions["runtime_profile"] = profile
    app.extensions["state_persistence"] = persistence
    app.extensions["db_writer"] = db_writer
    app.extensions["auth_verifier"] = verifier
    app.extensions["measurement_sessions"] = sessions
    if history_reader is _AUTO_HISTORY_READER:
        history_reader = StateHistoryReader()
    app.extensions["state_history_reader"] = history_reader

    def token_required(function):
        @wraps(function)
        def decorated(*args, **kwargs):
            try:
                token = bearer_token(request.headers.get("Authorization"))
                identity = verifier.verify(token)
            except AuthenticationError:
                return jsonify({
                    "v": 1,
                    "status": "error",
                    "error": {"code": "invalid_token", "message": "인증이 필요합니다."},
                }), 401
            except AuthenticationUnavailable:
                return jsonify({
                    "v": 1,
                    "status": "error",
                    "error": {
                        "code": "auth_unavailable",
                        "message": "인증 서비스를 사용할 수 없습니다.",
                    },
                }), 503
            return function(identity.user_id, *args, **kwargs)

        return decorated

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "realtime": True})

    @app.get("/api/state/history")
    @token_required
    def get_state_history(user_id):
        unsupported = sorted(set(request.args) - {"start", "end"})
        if unsupported:
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {
                    "code": "invalid_request",
                    "message": "지원하지 않는 query parameter입니다.",
                },
            }), 400

        with measurement_lock:
            measurement = sessions.active()
            if measurement is None or measurement.user_id != user_id:
                return jsonify({
                    "v": 1,
                    "status": "error",
                    "error": {
                        "code": "no_active_session",
                        "message": "활성 측정 세션이 없습니다.",
                    },
                }), 409

        try:
            start, end = resolve_history_period(
                request.args.get("start"),
                request.args.get("end"),
                now=history_now() if history_now is not None else None,
            )
        except HistoryRequestError as error:
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {"code": "invalid_time_range", "message": str(error)},
            }), 400

        try:
            baseline, states = history_reader.fetch(
                user_id,
                measurement.session_id,
                start,
                end,
            )
        except HistoryUnavailable as error:
            log.warning("state history 조회 실패: %s", error)
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {
                    "code": "history_unavailable",
                    "message": "최근 상태 기록을 불러올 수 없습니다.",
                },
            }), 503

        response = {
            "v": 1,
            "status": "success",
            "stream": {
                "user_id": str(user_id),
                "session_id": str(measurement.session_id),
            },
            "period": {
                "start": utc_iso8601(start),
                "end": utc_iso8601(end),
            },
            "states": states,
        }
        if baseline is not None:
            response["baseline"] = baseline

        message = _validation_message(STATE_HISTORY_VALIDATOR, response)
        if message:
            log.error("state history 응답 계약 위반: %s", message)
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {
                    "code": "invalid_history_response",
                    "message": "최근 상태 응답을 만들 수 없습니다.",
                },
            }), 500
        return jsonify(response)

    @app.post("/api/measurement/start")
    @token_required
    def start_measurement(user_id):
        try:
            with measurement_lock:
                measurement, created = sessions.start(user_id)
                if created:
                    pipeline.reset()
        except MeasurementInUse:
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {
                    "code": "measurement_in_use",
                    "message": "다른 측정 세션이 진행 중입니다.",
                },
            }), 409
        return jsonify({
            "v": 1,
            "status": "success",
            "measurement": measurement.as_dict(),
            "created": created,
        }), 201 if created else 200

    @app.post("/api/measurement/stop")
    @token_required
    def stop_measurement(user_id):
        try:
            with measurement_lock:
                measurement, already_stopped = sessions.stop(user_id)
                if persistence is not None:
                    persistence.end_session(user_id, measurement.session_id)
        except NoMeasurementSession:
            return jsonify({
                "v": 1,
                "status": "error",
                "error": {
                    "code": "no_active_session",
                    "message": "활성 측정 세션이 없습니다.",
                },
            }), 409
        return jsonify({
            "v": 1,
            "status": "success",
            "measurement": measurement.as_dict(),
            "already_stopped": already_stopped,
        })

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

    @socketio.on("connect")
    def handle_connect(auth):
        token = auth.get("token") if isinstance(auth, dict) else None
        if (
            isinstance(token, str)
            and isinstance(sensor_auth_token, str)
            and sensor_auth_token
            and hmac.compare_digest(token, sensor_auth_token)
        ):
            with socket_identity_lock:
                socket_identities[request.sid] = ("sensor", None)
            return True
        try:
            identity = verifier.verify(token)
        except (AuthenticationError, AuthenticationUnavailable):
            return False
        with socket_identity_lock:
            socket_identities[request.sid] = ("user", identity.user_id)
        join_room(f"user:{identity.user_id}")
        return True

    @socketio.on("disconnect")
    def handle_disconnect():
        with socket_identity_lock:
            socket_identities.pop(request.sid, None)

    @socketio.on("sensor_data")
    def handle_sensor_data(payload):
        with socket_identity_lock:
            socket_identity = socket_identities.get(request.sid)
        if socket_identity is None or socket_identity[0] != "sensor":
            log.warning("인증되지 않은 sensor_data 전송을 거부합니다")
            return
        try:
            with measurement_lock:
                pipeline.validate(payload)
                measurement = sessions.active()
                if measurement is None:
                    return
                decision = pipeline.process_validated(payload)
                if decision is None:
                    log.debug(
                        "이번 단계에서 처리하지 않는 source입니다: %s",
                        payload.get("source"),
                    )
                    return

                # DB보다 인증 사용자의 Front room이 먼저입니다.
                socketio.emit(
                    "state",
                    decision,
                    to=f"user:{measurement.user_id}",
                )
                if persistence is not None:
                    try:
                        persistence.handle(
                            payload,
                            decision,
                            user_id=measurement.user_id,
                            session_id=measurement.session_id,
                        )
                    except Exception as error:          # noqa: BLE001
                        log.warning("state_logs enqueue 실패: %s", error)
        except PayloadError as error:
            log.warning("payload 형식 오류, 이 샘플은 버립니다: %s", error)
            return
        except (KeyError, TypeError, ValueError) as error:
            log.warning("sensor_data 처리 오류, 이 샘플은 버립니다: %s", error)
            return

    return app, socketio


app, socketio = create_app()


if __name__ == "__main__":
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "5000"))
    print(f"Project Soma backend: http://{host}:{port}")
    try:
        socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
    finally:
        writer = app.extensions.get("db_writer")
        if writer is not None:
            writer.stop()
