"""Project Soma 실시간 백엔드.

수집 계층의 ``sensor_data`` 를 항상 계약으로 검증합니다. 인증 사용자의 measurement가
CALIBRATING일 때 Chair-authoritative merged samples로 session working baseline을 만들고,
MEASURING일 때만 fresh Vision과 병합한 값을 새 ``FusionState`` 에 반영합니다. state를
사용자 room에 먼저 발행하고 session-scoped Feedback Policy의 logical feedback을 발행한
뒤 비동기 persistence를 시도합니다.

실시간 경로는 Supabase와 독립적입니다. snapshot 저장은 비동기 DBWriter로 넘기고
History DB 조회는 해당 HTTP 요청에서만 수행하므로, DB 설정이나 인터넷 연결이 없어도
Chair → Front 경로는 계속 동작합니다.
"""
import hmac
import logging
import os
import sys
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.state import OCCUPANCY_MIN, FusionState, step  # noqa: E402
from feedback.config import feedback_config_for_mode  # noqa: E402
from server.auth import (  # noqa: E402
    AuthenticationError,
    AuthenticationUnavailable,
    SupabaseAuthVerifier,
    bearer_token,
)
from server.config import (  # noqa: E402
    DEFAULT_SENSOR_MERGE_POLICY,
    DEMO_PROFILE,
    RuntimeProfile,
    load_runtime_profile,
)
from server.db_writer import DBWriter  # noqa: E402
from server.feedback_coordinator import FeedbackCoordinator  # noqa: E402
from server.measurement_sessions import (  # noqa: E402
    MeasurementInUse,
    MeasurementSessionRegistry,
    NoMeasurementSession,
)
from server.measurement_calibration import (  # noqa: E402
    MeasurementCalibration,
    MeasurementPhase,
)
from server.state_history import (  # noqa: E402
    HistoryRequestError,
    HistoryUnavailable,
    StateHistoryReader,
    resolve_history_period,
    utc_iso8601,
)
from server.state_persistence import StatePersistence  # noqa: E402
from server.sensor_merge import SessionSensorCache  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

log = logging.getLogger("soma.server")

SENSOR_SCHEMA_PATH = ROOT / "docs" / "contracts" / "sensor_data.schema.json"
STATE_SCHEMA_PATH = ROOT / "docs" / "contracts" / "state.schema.json"
STATE_HISTORY_SCHEMA_PATH = ROOT / "docs" / "contracts" / "state_history.schema.json"
MEASUREMENT_PHASE_SCHEMA_PATH = (
    ROOT / "docs" / "contracts" / "measurement_phase.schema.json"
)
_AUTO_PERSISTENCE = object()
_AUTO_HISTORY_READER = object()
FEEDBACK_DEVICE_ROOM = "feedback_devices"
FEEDBACK_DEVICE_ROLE = "feedback_device"
CROSS_VALIDATION_EVENT = "cross_validation_observation"
CROSS_VALIDATION_ROOM_PREFIX = "cross_validation:"


def _cross_validation_room(user_id):
    return f"{CROSS_VALIDATION_ROOM_PREFIX}{user_id}"


def _cross_validation_observation(
    sensor_cache,
    chair_payload,
    decision,
    distance_evidence=None,
    distance_temporal=None,
):
    """Build opt-in eval instrumentation without changing production contracts."""
    latest_chair = sensor_cache.latest_chair
    latest_vision = sensor_cache.latest_vision
    receipt_age = sender_delta = None
    if latest_chair is not None and latest_vision is not None:
        receipt_age = max(
            latest_chair.received_at - latest_vision.received_at,
            0.0,
        )
        sender_delta = abs(
            latest_chair.sender_t - latest_vision.sender_t
        )
    observation = {
        "v": 1,
        "chair": chair_payload,
        "vision": None if latest_vision is None else latest_vision.payload,
        "vision_availability": sensor_cache.vision_availability.value,
        "vision_receipt_age_sec": (
            None if receipt_age is None else round(receipt_age, 6)
        ),
        "chair_vision_sender_delta_sec": (
            None if sender_delta is None else round(sender_delta, 6)
        ),
        "state": decision,
    }
    if distance_evidence is not None:
        observation["distance_evidence"] = distance_evidence.as_dict()
    if distance_temporal is not None:
        observation["distance_temporal"] = distance_temporal.as_dict()
    return observation


def _load_validator(path):
    """UTF-8 JSON schema를 읽어 Draft 2020-12 validator를 만듭니다."""
    import json

    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


SENSOR_VALIDATOR = _load_validator(SENSOR_SCHEMA_PATH)
STATE_VALIDATOR = _load_validator(STATE_SCHEMA_PATH)
STATE_HISTORY_VALIDATOR = _load_validator(STATE_HISTORY_SCHEMA_PATH)
MEASUREMENT_PHASE_VALIDATOR = _load_validator(MEASUREMENT_PHASE_SCHEMA_PATH)


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
    """Chair-authoritative Fusion state with a session-scoped Vision cache."""

    def __init__(
        self,
        timing=DEMO_PROFILE.fusion,
        load_config=DEMO_PROFILE.load,
        merge_policy=DEFAULT_SENSOR_MERGE_POLICY,
        monotonic=None,
        distance_timing=DEMO_PROFILE.distance_evidence_timing,
    ):
        self._state = FusionState()
        self._lock = threading.RLock()
        self._timing = timing
        self._load_config = load_config
        self._distance_timing = distance_timing
        cache_kwargs = {} if monotonic is None else {"monotonic": monotonic}
        self._sensor_cache = SessionSensorCache(merge_policy, **cache_kwargs)

    @property
    def sensor_cache(self):
        return self._sensor_cache

    def validate(self, payload):
        """Validate sensor_data without advancing Fusion state."""
        message = _validation_message(SENSOR_VALIDATOR, payload)
        if message:
            raise PayloadError(f"sensor_data 계약 위반: {message}")

    def process(self, payload):
        """Validate and process one source event using Chair-authoritative ticks."""
        self.validate(payload)
        return self.process_validated(payload)

    def process_validated(self, payload):
        """Advance Fusion for one payload already checked against the contract."""
        with self._lock:
            sample = self.prepare_validated(payload)
            if sample is None:
                return None
            return self.process_prepared(payload, sample)

    def prepare_validated(self, payload):
        """Cache Vision or build one Chair-authoritative merged sample."""
        with self._lock:
            if payload["source"] == "vision":
                self._sensor_cache.update_vision(payload)
                return None
            return self._sensor_cache.merged_chair_sample(payload)

    def mark_prepared(self, payload):
        """Commit a Chair ordering checkpoint without running Fusion."""
        with self._lock:
            self._sensor_cache.mark_chair_processed(payload)

    def process_prepared(self, payload, sample, *, distance_evidence=None):
        """Run Fusion for a previously prepared Chair sample."""
        with self._lock:
            current, decision = step(
                self._state,
                sample,
                payload["t"],
                timing=self._timing,
                load_config=self._load_config,
                distance_evidence=distance_evidence,
                distance_timing=self._distance_timing,
            )
            message = _validation_message(STATE_VALIDATOR, decision)
            if message:
                raise PayloadError(f"state 계약 위반: {message}")
            self._state = current
            self._sensor_cache.mark_chair_processed(payload)
        return decision

    def reset(self):
        """Start a measurement session with no prior accumulated Fusion state."""
        with self._lock:
            self._state = FusionState()
            self._sensor_cache.reset()

    def clear_cache(self):
        """Forget source samples immediately when an ACTIVE session stops."""
        with self._lock:
            self._sensor_cache.reset()


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
    sensor_merge_policy=DEFAULT_SENSOR_MERGE_POLICY,
    sensor_monotonic=None,
    feedback_coordinator=None,
    calibration_monotonic=None,
    calibration_wall_clock=None,
    calibration_timer_factory=None,
    _skip_calibration_for_testing=False,
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
    pipeline = ChairPipeline(
        profile.fusion,
        profile.load,
        merge_policy=sensor_merge_policy,
        monotonic=sensor_monotonic,
        distance_timing=profile.distance_evidence_timing,
    )
    verifier = auth_verifier or SupabaseAuthVerifier()
    sessions = session_registry or MeasurementSessionRegistry()
    feedback = feedback_coordinator or FeedbackCoordinator(
        feedback_config_for_mode(profile.name)
    )
    calibration = MeasurementCalibration(
        profile.calibration,
        occupancy_min=OCCUPANCY_MIN,
        distance_evidence_timing=profile.distance_evidence_timing,
    )
    calibration_monotonic = calibration_monotonic or time.monotonic
    calibration_wall_clock = calibration_wall_clock or time.time
    calibration_timer_factory = calibration_timer_factory or threading.Timer
    if _skip_calibration_for_testing and not testing:
        raise ValueError("calibration may only be skipped in tests")
    measurement_lock = threading.RLock()
    socket_identities = {}
    cross_validation_observers = {}
    socket_identity_lock = threading.Lock()
    latest_device_feedback = {"decision": None}
    device_feedback_lock = threading.Lock()
    calibration_timer = {"timer": None, "generation": None}
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
    app.extensions["sensor_cache"] = pipeline.sensor_cache
    app.extensions["runtime_profile"] = profile
    app.extensions["state_persistence"] = persistence
    app.extensions["db_writer"] = db_writer
    app.extensions["auth_verifier"] = verifier
    app.extensions["measurement_sessions"] = sessions
    app.extensions["feedback_coordinator"] = feedback
    app.extensions["measurement_calibration"] = calibration
    app.extensions["latest_device_feedback"] = latest_device_feedback
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

    def emit_device_off(*, to=FEEDBACK_DEVICE_ROOM):
        """Best-effort lifecycle control for the optional Nano bridge."""
        try:
            socketio.emit("feedback_device_off", {"v": 1}, to=to)
        except Exception as error:  # noqa: BLE001
            log.warning("feedback device OFF 전송 실패 (%s)", type(error).__name__)

    def reset_device_feedback():
        with device_feedback_lock:
            latest_device_feedback["decision"] = None
        emit_device_off()

    def cancel_calibration_timer():
        timer = calibration_timer["timer"]
        calibration_timer["timer"] = None
        calibration_timer["generation"] = None
        if timer is not None:
            timer.cancel()

    def phase_payload():
        return calibration.snapshot_for(
            calibration_monotonic(),
            calibration_wall_clock(),
        )

    def emit_phase(*, to):
        payload = phase_payload()
        message = _validation_message(MEASUREMENT_PHASE_VALIDATOR, payload)
        if message:
            raise PayloadError(f"measurement_phase 계약 위반: {message}")
        socketio.emit("measurement_phase", payload, to=to)
        return payload

    def reset_measurement_engines(measurement):
        """Ensure calibration observations cannot leak into measurement state."""
        pipeline.reset()
        feedback.stop_session(measurement.session_id)
        feedback.start_session(measurement.session_id)
        reset_device_feedback()
        if persistence is not None:
            persistence.end_session(measurement.user_id, measurement.session_id)

    def apply_calibration_evaluation(measurement, evaluation):
        """Apply sensor and timer evaluations through one exactly-once path."""
        if evaluation.became_ready:
            emit_phase(to=f"user:{measurement.user_id}")  # READY exactly once
            cancel_calibration_timer()
            reset_measurement_engines(measurement)
            if calibration.accept_ready():
                emit_phase(to=f"user:{measurement.user_id}")  # MEASURING once
            return
        if evaluation.became_failed:
            cancel_calibration_timer()
            reset_device_feedback()
            emit_phase(to=f"user:{measurement.user_id}")

    def calibration_timeout_callback(session_id, generation):
        with measurement_lock:
            measurement = sessions.active()
            if (
                measurement is None
                or str(measurement.session_id) != str(session_id)
                or calibration.session_id != str(session_id)
                or calibration.generation != generation
            ):
                return
            evaluation = calibration.evaluate(calibration_monotonic())
            apply_calibration_evaluation(measurement, evaluation)

    def schedule_calibration_timeout(measurement, generation):
        cancel_calibration_timer()
        timer = calibration_timer_factory(
            profile.calibration.timeout_sec,
            lambda: calibration_timeout_callback(
                measurement.session_id,
                generation,
            ),
        )
        if hasattr(timer, "daemon"):
            timer.daemon = True
        calibration_timer["timer"] = timer
        calibration_timer["generation"] = generation
        timer.start()

    def finalize_measurement(measurement):
        """Clear every session-scoped subsystem after an authenticated stop."""
        cancel_calibration_timer()
        calibration.stop()
        pipeline.reset()
        feedback.stop_session(measurement.session_id)
        reset_device_feedback()
        if persistence is not None:
            persistence.end_session(measurement.user_id, measurement.session_id)
        emit_phase(to=f"user:{measurement.user_id}")

    def has_user_socket(user_id):
        with socket_identity_lock:
            return any(
                role == "user" and connected_user_id == user_id
                for role, connected_user_id in socket_identities.values()
            )

    def has_cross_validation_observer(user_id):
        with socket_identity_lock:
            return user_id in cross_validation_observers.values()

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
                active = sessions.active()
                if (
                    active is not None
                    and active.user_id != user_id
                    and not has_user_socket(active.user_id)
                ):
                    orphaned, already_stopped = sessions.stop(active.user_id)
                    if not already_stopped:
                        finalize_measurement(orphaned)
                        log.info(
                            "연결된 owner socket이 없는 measurement를 정리했습니다"
                        )
                measurement, created = sessions.start(user_id)
                if created:
                    pipeline.reset()
                    feedback.start_session(measurement.session_id)
                    reset_device_feedback()
                    if _skip_calibration_for_testing:
                        calibration.force_measuring_for_test(measurement.session_id)
                        emit_phase(to=f"user:{measurement.user_id}")
                    else:
                        generation = calibration.start(
                            measurement.session_id,
                            calibration_monotonic(),
                        )
                        emit_phase(to=f"user:{measurement.user_id}")
                        schedule_calibration_timeout(measurement, generation)
                else:
                    emit_phase(to=f"user:{measurement.user_id}")
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
                if not already_stopped:
                    finalize_measurement(measurement)
                elif persistence is not None:
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
        requested_role = auth.get("role") if isinstance(auth, dict) else None
        observer_opt_in = (
            isinstance(auth, dict)
            and auth.get("observe_sensor_data") is True
        )
        if (
            isinstance(token, str)
            and isinstance(sensor_auth_token, str)
            and sensor_auth_token
            and hmac.compare_digest(token, sensor_auth_token)
        ):
            if requested_role not in (None, "sensor", FEEDBACK_DEVICE_ROLE):
                log.warning(
                    "socket connect rejected socket_sid=%s "
                    "category=invalid_device_role requested_role=%s "
                    "observer_opt_in=%s",
                    request.sid,
                    requested_role,
                    observer_opt_in,
                )
                return False
            role = requested_role or "sensor"
            with socket_identity_lock:
                socket_identities[request.sid] = (role, None)
            if role == FEEDBACK_DEVICE_ROLE:
                join_room(FEEDBACK_DEVICE_ROOM)
                with device_feedback_lock:
                    current = latest_device_feedback["decision"]
                try:
                    if current is None:
                        emit_device_off(to=request.sid)
                    else:
                        # Reconnect restores only LEVEL; an old ALERT must not replay.
                        synchronized = dict(current)
                        synchronized["transition"] = False
                        socketio.emit(
                            "feedback_device",
                            synchronized,
                            to=request.sid,
                        )
                except Exception as error:  # noqa: BLE001
                    log.warning(
                        "feedback device 동기화 실패 (%s)",
                        type(error).__name__,
                    )
            log.info(
                "socket connect accepted socket_sid=%s role=%s "
                "observer_opt_in=%s",
                request.sid,
                role,
                observer_opt_in,
            )
            return True
        try:
            identity = verifier.verify(token)
        except AuthenticationError:
            log.warning(
                "socket connect rejected socket_sid=%s "
                "category=invalid_user_token observer_opt_in=%s",
                request.sid,
                observer_opt_in,
            )
            return False
        except AuthenticationUnavailable:
            log.warning(
                "socket connect rejected socket_sid=%s "
                "category=auth_unavailable observer_opt_in=%s",
                request.sid,
                observer_opt_in,
            )
            return False
        try:
            with socket_identity_lock:
                socket_identities[request.sid] = ("user", identity.user_id)
                if observer_opt_in:
                    cross_validation_observers[request.sid] = identity.user_id
            join_room(f"user:{identity.user_id}")
            if observer_opt_in:
                join_room(_cross_validation_room(identity.user_id))
            with measurement_lock:
                active = sessions.active()
                if active is not None and active.user_id == identity.user_id:
                    emit_phase(to=request.sid)
        except Exception as error:  # noqa: BLE001
            with socket_identity_lock:
                socket_identities.pop(request.sid, None)
                cross_validation_observers.pop(request.sid, None)
            log.warning(
                "socket connect rejected socket_sid=%s "
                "category=room_setup_error observer_opt_in=%s error_type=%s",
                request.sid,
                observer_opt_in,
                type(error).__name__,
            )
            return False
        log.info(
            "socket connect accepted socket_sid=%s role=user "
            "observer_opt_in=%s",
            request.sid,
            observer_opt_in,
        )
        return True

    @socketio.on("disconnect")
    def handle_disconnect():
        with socket_identity_lock:
            disconnected = socket_identities.pop(request.sid, None)
            cross_validation_observers.pop(request.sid, None)
            owner_still_connected = (
                disconnected is not None
                and disconnected[0] == "user"
                and any(
                    role == "user" and user_id == disconnected[1]
                    for role, user_id in socket_identities.values()
                )
            )
        if (
            disconnected is None
            or disconnected[0] != "user"
            or owner_still_connected
        ):
            return

        # A measurement lease is held by at least one authenticated owner socket.
        # Releasing it on the last disconnect prevents an abandoned Chair stream
        # from continuing under the previous user's session after REST token expiry.
        user_id = disconnected[1]
        with measurement_lock:
            measurement = sessions.active()
            if measurement is None or measurement.user_id != user_id:
                return
            stopped, already_stopped = sessions.stop(user_id)
            if not already_stopped:
                finalize_measurement(stopped)
                log.info(
                    "마지막 인증 사용자 socket 종료로 measurement를 정리했습니다"
                )

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

                if calibration.phase is MeasurementPhase.CALIBRATING:
                    if calibration.failed:
                        return
                    sample = pipeline.prepare_validated(payload)
                    if sample is None:
                        return
                    evaluation = calibration.observe(
                        sample,
                        calibration_monotonic(),
                    )
                    pipeline.mark_prepared(payload)
                    if evaluation.became_ready or evaluation.became_failed:
                        apply_calibration_evaluation(measurement, evaluation)
                    else:
                        emit_phase(to=f"user:{measurement.user_id}")
                    return

                if calibration.phase is not MeasurementPhase.MEASURING:
                    return

                sample = pipeline.prepare_validated(payload)
                if sample is None:
                    return
                sample.update(calibration.relative_evidence(sample, now=payload["t"]))
                decision = pipeline.process_prepared(
                    payload,
                    sample,
                    distance_evidence=calibration.latest_distance_temporal,
                )
                if decision is None:
                    return

                # DB보다 인증 사용자의 Front room이 먼저입니다.
                socketio.emit(
                    "state",
                    decision,
                    to=f"user:{measurement.user_id}",
                )
                if has_cross_validation_observer(measurement.user_id):
                    try:
                        socketio.emit(
                            CROSS_VALIDATION_EVENT,
                            _cross_validation_observation(
                                pipeline.sensor_cache,
                                payload,
                                decision,
                                calibration.latest_distance_evidence,
                                calibration.latest_distance_temporal,
                            ),
                            to=_cross_validation_room(measurement.user_id),
                        )
                    except Exception as error:  # noqa: BLE001
                        log.warning(
                            "cross-validation observation 전송 실패 (%s)",
                            type(error).__name__,
                        )
                feedback_decision = None
                try:
                    feedback_decision = feedback.process_decision(
                        measurement.session_id,
                        decision,
                        payload["t"],
                    )
                except Exception as error:  # noqa: BLE001
                    log.warning(
                        "feedback policy 처리 실패 (%s)",
                        type(error).__name__,
                    )
                if feedback_decision is not None:
                    try:
                        socketio.emit(
                            "feedback",
                            feedback_decision,
                            to=f"user:{measurement.user_id}",
                        )
                    except Exception as error:  # noqa: BLE001
                        log.warning(
                            "user feedback 전송 실패 (%s)",
                            type(error).__name__,
                        )
                    with device_feedback_lock:
                        latest_device_feedback["decision"] = feedback_decision
                    try:
                        socketio.emit(
                            "feedback_device",
                            feedback_decision,
                            to=FEEDBACK_DEVICE_ROOM,
                        )
                    except Exception as error:  # noqa: BLE001
                        log.warning(
                            "feedback device 전송 실패 (%s)",
                            type(error).__name__,
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
