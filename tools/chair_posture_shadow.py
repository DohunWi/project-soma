#!/usr/bin/env python3
"""Live and offline runner for the independent posture shadow observer."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chair_activity_diagnostic import (  # noqa: E402
    chair_payload_parts,
    distribution,
    pressure_shape,
    pressure_values,
)
from tools.chair_posture_diagnostic import pressure_distribution  # noqa: E402
from tools.access_token import (  # noqa: E402
    AccessTokenLifetimeError,
    require_access_token_lifetime,
)
from tools.chair_e2e import (  # noqa: E402
    SOCKET_TRANSPORTS,
    connect_authenticated_socket,
    observer_required_lifetime,
)
from tools.posture_shadow import (  # noqa: E402
    SHADOW_DEFAULT_ANCHOR_SAMPLES,
    SHADOW_DEFAULT_ANCHOR_STABILITY_RADIUS,
    SHADOW_DEFAULT_LONG_ABSENCE_SEC,
    SHADOW_DEFAULT_MAX_GAP_SEC,
    PostureShadowObserver,
    ShadowConfig,
    ShadowMode,
)
from tools.posture_ir_shadow import (  # noqa: E402
    PRESSURE_IR_DEFAULT_ANCHOR_IR_RADIUS_MM,
    PRESSURE_IR_DEFAULT_ANCHOR_PRESSURE_RADIUS,
    PRESSURE_IR_DEFAULT_ANCHOR_SAMPLES,
    PRESSURE_IR_DEFAULT_CANDIDATE_SEC,
    PRESSURE_IR_DEFAULT_FORWARD_FRONT_BACK_DELTA,
    PRESSURE_IR_DEFAULT_FORWARD_IR_DELTA_MM,
    PRESSURE_IR_DEFAULT_LATERAL_GUARD_DELTA,
    PRESSURE_IR_DEFAULT_MAX_GAP_SEC,
    PRESSURE_IR_DEFAULT_RECOVERY_FRONT_BACK_DELTA,
    PRESSURE_IR_DEFAULT_RECOVERY_IR_DELTA_MM,
    PRESSURE_IR_DEFAULT_RECOVERY_PRESSURE_DISTANCE,
    PRESSURE_IR_DEFAULT_RECOVERY_SEC,
    PressureIrPostureShadowObserver,
    PressureIrShadowConfig,
    PressureIrShadowMode,
)


DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
DEFAULT_DURATION_SEC = 180.0
SOCKET_NAMESPACE = "/"
IR_OUT_OF_RANGE_MM = 8191
PRESSURE_NAMES = ("fl", "fr", "bl", "br")


class ShadowConnectionError(RuntimeError):
    """Credential-safe failure with a connection stage for the operator."""


class ShadowConnectionStatus:
    """Track transport/auth subscription and observation as separate stages."""

    def __init__(self):
        self.namespace_connected = False
        self.connect_error_received = False
        self.connect_error_payload_type = None
        self.latest_phase = None
        self.latest_calibration_status = None
        self.observation_count = 0

    def handle_connect(self):
        self.namespace_connected = True

    def handle_connect_error(self, payload):
        self.connect_error_received = True
        self.connect_error_payload_type = type(payload).__name__

    def note_phase(self, payload):
        if not isinstance(payload, dict):
            return
        self.latest_phase = payload.get("phase")
        calibration = payload.get("calibration")
        self.latest_calibration_status = (
            calibration.get("status") if isinstance(calibration, dict) else None
        )

    def note_observation(self):
        self.observation_count += 1

    def observation_summary(self):
        if self.observation_count:
            return (
                "[posture-shadow] observation status=received "
                f"count={self.observation_count}"
            )
        phase = self.latest_phase or "UNSEEN"
        if phase != "MEASURING":
            return (
                "[posture-shadow] observation status=not_received "
                f"phase={phase}; connection/subscription succeeded, but "
                "cross_validation_observation requires the same user's "
                "MEASURING session and Chair-authoritative ticks"
            )
        return (
            "[posture-shadow] observation status=not_received phase=MEASURING; "
            "verify Chair sensor ticks for the authenticated session"
        )


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def extract_chair_ir(chair):
    """Return the first normalized Chair IR value and a credential-safe shape."""
    ir = chair.get("ir") if isinstance(chair, dict) else None
    shape = {
        "ir_type": type(ir).__name__,
        "ir_length": len(ir) if isinstance(ir, (list, tuple)) else None,
        "ir_value_type": (
            type(ir[0]).__name__
            if isinstance(ir, (list, tuple)) and ir
            else None
        ),
    }
    if not isinstance(ir, (list, tuple)) or not ir:
        return None, shape, "missing"
    return ir[0], shape, None


def inspect_chair_ir(value, *, seated, previous_valid_mm=None):
    """Classify IR for diagnostics without changing production validity rules."""
    status = "valid"
    if value is None:
        status = "null"
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        status = "invalid_type"
    elif not math.isfinite(value):
        status = "non_finite"
    elif value == -1:
        status = "unavailable"
    elif value < 0:
        status = "invalid_negative"
    elif value == 0:
        status = "zero"
    elif value >= IR_OUT_OF_RANGE_MM:
        status = "out_of_range_sentinel"

    analysis_valid = status == "valid"
    analysis_usable = analysis_valid and seated
    delta = (
        value - previous_valid_mm
        if analysis_usable and previous_valid_mm is not None
        else None
    )
    return {
        "chair_distance_mm": value,
        "chair_ir_status": status,
        "chair_ir_analysis_valid": analysis_valid,
        "chair_ir_analysis_usable": analysis_usable,
        "chair_ir_delta_mm": delta,
        "chair_ir_abs_delta_mm": None if delta is None else abs(delta),
    }


class ShadowLogRecorder:
    """Map Backend observation rows to shadow output without mutating sources."""

    def __init__(self, sink, observer, pressure_ir_observer=None):
        self.sink = sink
        self.observer = observer
        self.pressure_ir_observer = (
            PressureIrPostureShadowObserver()
            if pressure_ir_observer is None
            else pressure_ir_observer
        )
        self.session_id = observer.session_id
        self.measurement_phase = None
        self._previous_ir_mm = None
        self._previous_ir_timestamp = None

    def handle_phase(self, payload):
        active = payload.get("active") is True
        session_id = payload.get("session_id") if active else None
        changed = self.observer.set_session(session_id)
        self.pressure_ir_observer.set_session(session_id)
        self.session_id = session_id
        self.measurement_phase = payload.get("phase")
        if changed:
            self._reset_ir_history()
        row = {
            "record_type": "shadow_session",
            "recorded_at": utc_now_iso(),
            "phase": payload.get("phase"),
            "active": active,
            "session_id": session_id,
            "shadow_reset": changed,
        }
        self._write(row)
        return row

    def record_observation(self, observation):
        chair_payload, chair = chair_payload_parts(observation)
        pressure = pressure_values(chair)
        chair_ir, ir_shape, ir_extract_status = extract_chair_ir(chair)
        state = observation.get("state") or {}
        metrics = state.get("metrics") or {}
        vision_payload = observation.get("vision")
        vision = (
            vision_payload.get("vision")
            if isinstance(vision_payload, dict)
            else None
        )
        vision = vision if isinstance(vision, dict) else {}
        diagnostics = {
            "vision": {
                "timestamp": (
                    vision_payload.get("t")
                    if isinstance(vision_payload, dict)
                    else None
                ),
                "availability": observation.get("vision_availability"),
                "receipt_age_sec": observation.get("vision_receipt_age_sec"),
                "chair_sender_delta_sec": observation.get(
                    "chair_vision_sender_delta_sec"
                ),
                "face_detected": vision.get("face_detected"),
                "blink": vision.get("blink"),
                "blink_rate": vision.get("blink_rate"),
                "blink_count": vision.get("blink_count"),
                "window_sec": vision.get("window_sec"),
                "blink_duration_ms": vision.get("blink_duration_ms"),
                "detect_rate": vision.get("detect_rate"),
                "yaw_dropped_rate": vision.get("yaw_dropped_rate"),
                "ear_threshold_mode": vision.get("ear_threshold_mode"),
                "open_ear_baseline": vision.get("open_ear_baseline"),
                "ear_calibration_status": vision.get(
                    "ear_calibration_status"
                ),
                "face_distance_cm": vision.get("face_distance_cm"),
            },
            "posture_tracking": observation.get("posture_tracking"),
            "static_burden": observation.get("static_burden"),
            "blink_policy": observation.get("blink_policy"),
            "distance_evidence": observation.get("distance_evidence"),
            "distance_temporal": observation.get("distance_temporal"),
            "penalties": observation.get("penalties"),
            "fusion": {
                "timestamp": state.get("t"),
                "state": state.get("state"),
                "score": state.get("score"),
                "confidence": state.get("confidence"),
                "reasons": state.get("reasons"),
                "metrics": metrics,
            },
        }
        return self.record_sample(
            timestamp=(
                chair_payload.get("t") if isinstance(chair_payload, dict) else None
            ),
            pressure=pressure,
            seated=metrics.get("seated") is True,
            static_hold_sec=metrics.get("static_hold_sec"),
            source_stage=None,
            chair_ir=chair_ir,
            ir_shape=ir_shape,
            ir_extract_status=ir_extract_status,
            observed_shape=(
                None
                if pressure is not None
                else pressure_shape(chair_payload, chair)
            ),
            diagnostics=diagnostics,
        )

    def record_sample(
        self,
        *,
        timestamp,
        pressure,
        seated,
        static_hold_sec,
        source_stage,
        chair_ir=None,
        ir_shape=None,
        ir_extract_status=None,
        observed_shape=None,
        diagnostics=None,
    ):
        result = self.observer.observe(
            timestamp,
            pressure,
            seated=seated,
            static_hold_sec=static_hold_sec,
            session_id=self.session_id,
        )
        pressure_ir_result = self.pressure_ir_observer.observe(
            timestamp,
            pressure,
            chair_ir,
            seated=seated,
            pressure_shadow=result,
            session_id=self.session_id,
        )
        interval = self._sample_interval(timestamp)
        continuous = (
            interval is not None
            and 0 < interval <= self.observer.config.shadow_max_gap_sec
        )
        previous_ir = self._previous_ir_mm if continuous else None
        ir = inspect_chair_ir(
            chair_ir,
            seated=seated,
            previous_valid_mm=previous_ir,
        )
        if ir_extract_status is not None:
            ir["chair_ir_status"] = ir_extract_status
            ir["chair_ir_analysis_valid"] = False
            ir["chair_ir_analysis_usable"] = False
            ir["chair_ir_delta_mm"] = None
            ir["chair_ir_abs_delta_mm"] = None

        features = (
            None if pressure is None else pressure_distribution(pressure)
        )
        row = {
            "record_type": "shadow_sample",
            "recorded_at": utc_now_iso(),
            "timestamp": timestamp,
            "session_id": self.session_id,
            "measurement_phase": self.measurement_phase,
            "source_stage": source_stage,
            "seated": seated,
            "valid_pressure": pressure is not None,
            "pressure": None if pressure is None else list(pressure),
            "fl": None if pressure is None else pressure[0],
            "fr": None if pressure is None else pressure[1],
            "bl": None if pressure is None else pressure[2],
            "br": None if pressure is None else pressure[3],
            "pressure_sum": None if features is None else features["pressure_sum"],
            "valid_distribution": (
                False if features is None else features["valid"]
            ),
            "distribution_invalid_reason": (
                "pressure_must_have_four_finite_numbers"
                if features is None
                else features["reason"]
            ),
            "channel_share": None if features is None else features["channel_share"],
            "left_right_ratio": (
                None if features is None else features["left_right_ratio"]
            ),
            "front_back_ratio": (
                None if features is None else features["front_back_ratio"]
            ),
            **ir,
            "chair_ir_observed_shape": ir_shape,
            "chair_sample_interval_sec": interval,
            "pressure_ir_shared_payload_timestamp": True,
            "pressure_ir_sensor_offset_sec": None,
            "observed_shape": observed_shape,
            "shadow_state": result.get("shadow_state"),
            "shadow": result,
            "pressure_ir_shadow": pressure_ir_result,
        }
        if diagnostics is not None:
            row.update(diagnostics)
        self._update_ir_history(timestamp, ir)
        self._write(row)
        if result.get("shadow_event") is not None:
            self._write(
                {
                    "record_type": "shadow_event",
                    "recorded_at": utc_now_iso(),
                    "source_stage": source_stage,
                    "event": result["shadow_event"],
                }
            )
        if pressure_ir_result.get("transition") is not None:
            self._write(
                {
                    "record_type": "pressure_ir_shadow_event",
                    "recorded_at": utc_now_iso(),
                    "source_stage": source_stage,
                    "event": pressure_ir_result["transition"],
                }
            )
        return row

    def _sample_interval(self, timestamp):
        if not _finite_number(timestamp) or not _finite_number(
            self._previous_ir_timestamp
        ):
            return None
        return timestamp - self._previous_ir_timestamp

    def _update_ir_history(self, timestamp, ir):
        self._previous_ir_timestamp = timestamp if _finite_number(timestamp) else None
        self._previous_ir_mm = (
            ir["chair_distance_mm"]
            if ir["chair_ir_analysis_usable"]
            else None
        )

    def _reset_ir_history(self):
        self._previous_ir_mm = None
        self._previous_ir_timestamp = None

    def record_source_transition(self, source_row):
        row = {
            "record_type": "source_stage_transition",
            "recorded_at": utc_now_iso(),
            "from_stage": source_row.get("from_stage"),
            "to_stage": source_row.get("to_stage"),
            "source_experiment_elapsed_sec": source_row.get(
                "experiment_elapsed_sec"
            ),
        }
        self._write(row)
        return row

    def _write(self, row):
        self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.sink.flush()


def replay_file(source_path, recorder, *, session_id=None):
    """Replay R19-2 JSONL in original timestamp order without sleeping."""
    replay_session = session_id or f"offline:{Path(source_path).name}"
    recorder.session_id = replay_session
    recorder.observer.set_session(replay_session)
    last_timestamp = None
    sample_count = 0
    with Path(source_path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL at line {line_number}") from error
            if row.get("record_type") == "stage_transition":
                recorder.record_source_transition(row)
                continue
            if row.get("record_type") not in {"sample", "shadow_sample"}:
                continue
            timestamp = row.get("timestamp")
            if (
                isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and math.isfinite(timestamp)
            ):
                if last_timestamp is not None and timestamp < last_timestamp:
                    raise ValueError(
                        f"source timestamp order regressed at line {line_number}"
                    )
                last_timestamp = timestamp
            pressure = None
            if row.get("valid_pressure") is True:
                values = row.get("pressure")
                if not isinstance(values, (list, tuple)):
                    values = [
                        row.get(name) for name in ("fl", "fr", "bl", "br")
                    ]
                pressure = pressure_values({"pressure": values})
            recorder.record_sample(
                timestamp=timestamp,
                pressure=pressure,
                seated=(
                    row.get("seated") is True
                    if row.get("record_type") == "shadow_sample"
                    else row.get("fusion_seated") is True
                ),
                static_hold_sec=(
                    (row.get("shadow") or {}).get("static_hold_sec")
                    if row.get("record_type") == "shadow_sample"
                    else row.get("static_hold_sec")
                ),
                source_stage=row.get("source_stage", row.get("stage")),
                chair_ir=row.get("chair_distance_mm"),
            )
            sample_count += 1
    return sample_count


def load_rows(path):
    rows = []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL at line {line_number}") from error
    return rows


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _ir_summary(samples):
    usable = [
        row["chair_distance_mm"]
        for row in samples
        if row.get("chair_ir_analysis_usable") is True
    ]
    delta = [
        row["chair_ir_delta_mm"]
        for row in samples
        if _finite_number(row.get("chair_ir_delta_mm"))
    ]
    absolute_delta = [abs(value) for value in delta]
    return {
        "status_counts": dict(
            Counter(row.get("chair_ir_status") for row in samples)
        ),
        "analysis_usable_samples": len(usable),
        "chair_distance_mm": distribution(usable),
        "chair_ir_delta_mm": distribution(delta),
        "chair_ir_abs_delta_mm": distribution(absolute_delta),
    }


def analyze_rows(rows):
    samples = [row for row in rows if row.get("record_type") == "shadow_sample"]
    events = [row for row in rows if row.get("record_type") == "shadow_event"]
    pressure_ir_events = [
        row
        for row in rows
        if row.get("record_type") == "pressure_ir_shadow_event"
    ]
    shadows = [row.get("shadow") or {} for row in samples]
    pressure_ir_shadows = [row.get("pressure_ir_shadow") or {} for row in samples]
    distances = [
        shadow["distance_from_anchor"]
        for shadow in shadows
        if isinstance(shadow.get("distance_from_anchor"), (int, float))
    ]
    candidate_elapsed = [
        shadow["candidate_elapsed_sec"]
        for shadow in shadows
        if isinstance(shadow.get("candidate_elapsed_sec"), (int, float))
        and shadow["candidate_elapsed_sec"] > 0
    ]
    stage_labels = list(
        dict.fromkeys(
            row.get("source_stage")
            for row in samples
            if row.get("source_stage") is not None
        )
    )
    stage_summary = {}
    for label in stage_labels:
        selected = [row for row in samples if row.get("source_stage") == label]
        selected_shadows = [row.get("shadow") or {} for row in selected]
        stage_summary[label] = {
            "samples": len(selected),
            "shadow_states": dict(
                Counter(shadow.get("shadow_state") for shadow in selected_shadows)
            ),
            "distance_from_anchor": distribution(
                [
                    shadow["distance_from_anchor"]
                    for shadow in selected_shadows
                    if isinstance(shadow.get("distance_from_anchor"), (int, float))
                ]
            ),
            "static_hold_sec": distribution(
                [
                    shadow["static_hold_sec"]
                    for shadow in selected_shadows
                    if isinstance(shadow.get("static_hold_sec"), (int, float))
                ]
            ),
            "confirmed_events": sum(
                shadow.get("shadow_event") is not None
                for shadow in selected_shadows
            ),
            "chair_ir": _ir_summary(selected),
        }
    return {
        "v": 1,
        "samples": len(samples),
        "valid_pressure_samples": sum(
            row.get("valid_pressure") is True for row in samples
        ),
        "shadow_modes": dict(Counter(shadow.get("shadow_mode") for shadow in shadows)),
        "shadow_states": dict(Counter(shadow.get("shadow_state") for shadow in shadows)),
        "invalid_reasons": dict(
            Counter(
                shadow.get("invalid_reason")
                for shadow in shadows
                if shadow.get("invalid_reason") is not None
            )
        ),
        "distance_from_anchor": distribution(distances),
        "candidate_elapsed_sec": distribution(candidate_elapsed),
        "confirmed_event_count": len(events),
        "confirmed_events": [row.get("event") for row in events],
        "pressure_ir_shadow": {
            "modes": dict(
                Counter(result.get("mode") for result in pressure_ir_shadows)
            ),
            "states": dict(
                Counter(result.get("state") for result in pressure_ir_shadows)
            ),
            "classifications": dict(
                Counter(
                    result.get("classification") for result in pressure_ir_shadows
                )
            ),
            "forward_confirmed_samples": sum(
                result.get("forward_confirmed") is True
                for result in pressure_ir_shadows
            ),
            "transition_count": len(pressure_ir_events),
            "transitions": [row.get("event") for row in pressure_ir_events],
        },
        "chair_ir": _ir_summary(samples),
        "pressure_ir_timing": {
            "shared_payload_timestamp": True,
            "per_sensor_timestamp_available": False,
        },
        "source_stage_summary": stage_summary,
        "stage_labels_used_by_observer": False,
        "production_behavior_changed": False,
    }


def write_summary(log_path, summary):
    summary_path = Path(log_path).with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def config_from_args(args, parser):
    try:
        return ShadowConfig(
            shadow_mode=ShadowMode(args.shadow_mode),
            shadow_anchor_samples=args.shadow_anchor_samples,
            shadow_anchor_stability_radius=args.shadow_anchor_radius,
            shadow_max_gap_sec=args.shadow_max_gap_sec,
            shadow_long_absence_sec=args.shadow_long_absence_sec,
            experimental_candidate_distance=args.experimental_candidate_distance,
            experimental_candidate_sec=args.experimental_candidate_sec,
            experimental_candidate_radius=args.experimental_candidate_radius,
        )
    except ValueError as error:
        parser.error(str(error))


def pressure_ir_config_from_args(args, parser):
    try:
        return PressureIrShadowConfig(
            mode=PressureIrShadowMode(args.pressure_ir_shadow_mode),
            anchor_samples=args.pressure_ir_anchor_samples,
            anchor_ir_radius_mm=args.pressure_ir_anchor_ir_radius_mm,
            anchor_pressure_radius=args.pressure_ir_anchor_pressure_radius,
            max_gap_sec=args.pressure_ir_max_gap_sec,
            forward_ir_delta_mm=args.experimental_forward_ir_delta_mm,
            forward_front_back_delta=(
                args.experimental_forward_front_back_delta
            ),
            lateral_guard_delta=args.experimental_lateral_guard_delta,
            candidate_sec=args.experimental_forward_candidate_sec,
            recovery_ir_delta_mm=args.experimental_recovery_ir_delta_mm,
            recovery_front_back_delta=(
                args.experimental_recovery_front_back_delta
            ),
            recovery_pressure_distance=(
                args.experimental_recovery_pressure_distance
            ),
            recovery_sec=args.experimental_recovery_sec,
        )
    except ValueError as error:
        parser.error(str(error))


def add_shadow_arguments(parser):
    parser.add_argument(
        "--shadow-mode",
        choices=[mode.value for mode in ShadowMode],
        default=ShadowMode.OBSERVE_ONLY.value,
    )
    parser.add_argument(
        "--shadow-anchor-samples",
        type=int,
        default=SHADOW_DEFAULT_ANCHOR_SAMPLES,
    )
    parser.add_argument(
        "--shadow-anchor-radius",
        type=float,
        default=SHADOW_DEFAULT_ANCHOR_STABILITY_RADIUS,
    )
    parser.add_argument(
        "--shadow-max-gap-sec",
        type=float,
        default=SHADOW_DEFAULT_MAX_GAP_SEC,
    )
    parser.add_argument(
        "--shadow-long-absence-sec",
        type=float,
        default=SHADOW_DEFAULT_LONG_ABSENCE_SEC,
    )
    parser.add_argument("--experimental-candidate-distance", type=float)
    parser.add_argument("--experimental-candidate-sec", type=float)
    parser.add_argument("--experimental-candidate-radius", type=float)


def add_pressure_ir_shadow_arguments(parser):
    parser.add_argument(
        "--pressure-ir-shadow-mode",
        choices=[mode.value for mode in PressureIrShadowMode],
        default=PressureIrShadowMode.OBSERVE_ONLY.value,
        help="diagnostic-only pressure+IR observer; detection requires experimental",
    )
    parser.add_argument(
        "--pressure-ir-anchor-samples",
        type=int,
        default=PRESSURE_IR_DEFAULT_ANCHOR_SAMPLES,
    )
    parser.add_argument(
        "--pressure-ir-anchor-ir-radius-mm",
        type=float,
        default=PRESSURE_IR_DEFAULT_ANCHOR_IR_RADIUS_MM,
    )
    parser.add_argument(
        "--pressure-ir-anchor-pressure-radius",
        type=float,
        default=PRESSURE_IR_DEFAULT_ANCHOR_PRESSURE_RADIUS,
    )
    parser.add_argument(
        "--pressure-ir-max-gap-sec",
        type=float,
        default=PRESSURE_IR_DEFAULT_MAX_GAP_SEC,
    )
    parser.add_argument(
        "--experimental-forward-ir-delta-mm",
        type=float,
        default=PRESSURE_IR_DEFAULT_FORWARD_IR_DELTA_MM,
    )
    parser.add_argument(
        "--experimental-forward-front-back-delta",
        type=float,
        default=PRESSURE_IR_DEFAULT_FORWARD_FRONT_BACK_DELTA,
    )
    parser.add_argument(
        "--experimental-lateral-guard-delta",
        type=float,
        default=PRESSURE_IR_DEFAULT_LATERAL_GUARD_DELTA,
    )
    parser.add_argument(
        "--experimental-forward-candidate-sec",
        type=float,
        default=PRESSURE_IR_DEFAULT_CANDIDATE_SEC,
    )
    parser.add_argument(
        "--experimental-recovery-ir-delta-mm",
        type=float,
        default=PRESSURE_IR_DEFAULT_RECOVERY_IR_DELTA_MM,
    )
    parser.add_argument(
        "--experimental-recovery-front-back-delta",
        type=float,
        default=PRESSURE_IR_DEFAULT_RECOVERY_FRONT_BACK_DELTA,
    )
    parser.add_argument(
        "--experimental-recovery-pressure-distance",
        type=float,
        default=PRESSURE_IR_DEFAULT_RECOVERY_PRESSURE_DISTANCE,
    )
    parser.add_argument(
        "--experimental-recovery-sec",
        type=float,
        default=PRESSURE_IR_DEFAULT_RECOVERY_SEC,
    )


def build_parser():
    parser = argparse.ArgumentParser(description="R19 posture change Shadow Mode")
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_SEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replay", type=Path, help="R19-2 JSONL offline replay")
    parser.add_argument("--analyze", type=Path, help="existing shadow JSONL analysis")
    add_shadow_arguments(parser)
    add_pressure_ir_shadow_arguments(parser)
    return parser


def default_output_path(prefix="chair_posture_shadow"):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "logs" / f"{prefix}_{stamp}.jsonl"


def connection_failure_stage(status):
    """Classify the farthest safe stage reached without exposing auth data."""
    if status.connect_error_received:
        return "socketio_namespace_auth_or_room_setup"
    if status.namespace_connected:
        return "post_connect"
    return "engineio_or_socketio_handshake"


def run_live_observer(
    client,
    recorder,
    *,
    url,
    token,
    duration,
    monotonic=time.monotonic,
    sleeper=time.sleep,
    write=print,
):
    """Connect read-only, collect observations, and always disconnect safely."""
    status = ShadowConnectionStatus()

    def handle_phase(payload):
        status.note_phase(payload)
        return recorder.handle_phase(payload)

    def handle_observation(payload):
        status.note_observation()
        return recorder.record_observation(payload)

    client.on("connect", status.handle_connect)
    client.on("connect_error", status.handle_connect_error)
    client.on("measurement_phase", handle_phase)
    client.on("cross_validation_observation", handle_observation)
    transports = ",".join(SOCKET_TRANSPORTS)
    write(
        f"[posture-shadow] connecting url={url} namespace={SOCKET_NAMESPACE} "
        f"transport={transports} observer_opt_in=true"
    )
    connected = False
    try:
        try:
            connect_authenticated_socket(
                client,
                url,
                token,
                observe_sensor_data=True,
            )
            connected = True
            status.namespace_connected = True
        except Exception as error:  # Socket.IO exposes version-specific types.
            stage = connection_failure_stage(status)
            detail = (
                f" connect_error_payload_type={status.connect_error_payload_type}"
                if status.connect_error_payload_type is not None
                else ""
            )
            raise ShadowConnectionError(
                f"connection failed stage={stage} error={type(error).__name__} "
                f"namespace={SOCKET_NAMESPACE} transport={transports}{detail}; "
                "check Backend socket rejection category and use a fresh Front "
                "Supabase access token"
            ) from error

        write(
            f"[posture-shadow] connected namespace={SOCKET_NAMESPACE} "
            f"transport={transports} observer_opt_in=true"
        )
        started = monotonic()
        while monotonic() - started < duration:
            sleeper(min(0.2, duration))
    finally:
        if connected:
            write(status.observation_summary())
        if client.connected:
            client.disconnect()
    return status


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analyze and args.replay:
        parser.error("--analyze and --replay are mutually exclusive")
    if args.analyze:
        summary = analyze_rows(load_rows(args.analyze))
        path = write_summary(args.analyze, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[posture-shadow] summary={path}")
        return 0
    config = config_from_args(args, parser)
    pressure_ir_config = pressure_ir_config_from_args(args, parser)
    output = args.output or default_output_path(
        "chair_posture_shadow_replay" if args.replay else "chair_posture_shadow"
    )

    if args.replay:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("x", encoding="utf-8", buffering=1) as sink:
                recorder = ShadowLogRecorder(
                    sink,
                    PostureShadowObserver(config),
                    PressureIrPostureShadowObserver(pressure_ir_config),
                )
                count = replay_file(args.replay, recorder)
        except Exception as error:  # noqa: BLE001
            print(
                f"[posture-shadow] replay failed ({type(error).__name__})",
                file=sys.stderr,
            )
            return 1
        summary_path = write_summary(output, analyze_rows(load_rows(output)))
        print(f"[posture-shadow] replayed_samples={count}")
        print(f"[posture-shadow] log={output}")
        print(f"[posture-shadow] summary={summary_path}")
        return 0

    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be a positive finite number")
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    token = os.getenv(args.token_env)
    if not token:
        parser.error(f"환경변수 {args.token_env}에 Supabase access token이 필요합니다")
    required_lifetime = observer_required_lifetime(args.duration)
    try:
        token_timing = require_access_token_lifetime(token, required_lifetime)
    except AccessTokenLifetimeError as error:
        print(
            f"[posture-shadow] authentication preflight failed: {error}",
            file=sys.stderr,
        )
        return 2
    print(
        "[posture-shadow] authentication preflight=ok "
        f"required_sec={math.ceil(required_lifetime)} "
        f"remaining_sec={max(math.floor(token_timing.remaining_sec), 0)}"
    )
    try:
        import socketio
    except ImportError:
        parser.error("python-socketio가 설치되어 있지 않습니다")
    output.parent.mkdir(parents=True, exist_ok=True)
    client = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
    try:
        with output.open("x", encoding="utf-8", buffering=1) as sink:
            recorder = ShadowLogRecorder(
                sink,
                PostureShadowObserver(config),
                PressureIrPostureShadowObserver(pressure_ir_config),
            )
            print(
                f"[posture-shadow] mode={config.shadow_mode.value} "
                f"pressure_ir_mode={pressure_ir_config.mode.value} "
                f"duration={args.duration:.0f}s"
            )
            run_live_observer(
                client,
                recorder,
                url=args.url,
                token=token,
                duration=args.duration,
            )
    except KeyboardInterrupt:
        print("[posture-shadow] interrupted; JSONL preserved")
    except ShadowConnectionError as error:
        print(f"[posture-shadow] {error}; credentials hidden", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001
        print(
            f"[posture-shadow] runtime failed stage=recording "
            f"error={type(error).__name__}; credentials hidden",
            file=sys.stderr,
        )
        return 1
    summary_path = write_summary(output, analyze_rows(load_rows(output)))
    print(f"[posture-shadow] log={output}")
    print(f"[posture-shadow] summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
