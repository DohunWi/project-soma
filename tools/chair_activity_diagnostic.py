#!/usr/bin/env python3
"""Read-only R18 Chair pressure activity recorder and analyzer."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.state import OCCUPANCY_MIN, STATIC_EPS  # noqa: E402
from tools.chair_e2e import connect_authenticated_socket  # noqa: E402

DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
DEFAULT_RECORD_SEC = 60.0
DEFAULT_GAP_SEC = 2.5
PRESSURE_NAMES = ("fl", "fr", "bl", "br")
STAGES = (
    ("STILL", "의자에 앉아 최대한 움직이지 마세요."),
    ("SMALL_SHIFT", "일상적인 범위에서 작게 체중을 이동하세요."),
    ("CLEAR_MOVE", "좌우 이동 등 명확한 자세 변경을 반복하세요."),
)


def pressure_values(payload):
    pressure = (payload or {}).get("pressure")
    if not isinstance(pressure, (list, tuple)) or len(pressure) != 4:
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in pressure
    ):
        return None
    return tuple(pressure)


def chair_payload_parts(observation):
    """Return the emitted Chair sensor payload and its nested Chair fields."""
    payload = observation.get("chair")
    if not isinstance(payload, dict):
        return payload, None
    chair = payload.get("chair")
    return payload, chair if isinstance(chair, dict) else None


def pressure_shape(payload, chair):
    """Describe only field shapes/types; never include values or credentials."""
    pressure = chair.get("pressure") if isinstance(chair, dict) else None
    return {
        "chair_payload_type": type(payload).__name__,
        "chair_payload_keys": (
            sorted(str(key) for key in payload) if isinstance(payload, dict) else []
        ),
        "chair_fields_type": type(chair).__name__,
        "chair_fields_keys": (
            sorted(str(key) for key in chair) if isinstance(chair, dict) else []
        ),
        "pressure_type": type(pressure).__name__,
        "pressure_length": (
            len(pressure) if isinstance(pressure, (list, tuple)) else None
        ),
        "pressure_value_types": (
            [type(value).__name__ for value in pressure]
            if isinstance(pressure, (list, tuple))
            else []
        ),
    }


def activity_value(current, previous):
    """Equal ``fusion.state._activity`` after a real baseline exists."""
    if current is None or previous is None or len(current) != len(previous):
        return None
    return sum(abs(a - b) for a, b in zip(current, previous))


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution(values):
    if not values:
        return {"median": None, "p90": None, "p95": None, "max": None}
    return {
        "median": statistics.median(values),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": max(values),
    }


class ActivityRecorder:
    """Transform opt-in Backend observations into line-buffered JSONL."""

    def __init__(self, sink, *, monotonic=time.monotonic, gap_sec=DEFAULT_GAP_SEC):
        self.sink = sink
        self.monotonic = monotonic
        self.gap_sec = gap_sec
        self.stage = None
        self.stage_started = None
        self.previous_pressure = None
        self.previous_received = None
        self.previous_static_hold = None

    def start_stage(self, label):
        self.stage = label
        self.stage_started = self.monotonic()

    def stop_stage(self):
        self.stage = None
        self.stage_started = None

    def record(self, observation):
        received = self.monotonic()
        chair_payload, chair = chair_payload_parts(observation)
        pressure = pressure_values(chair)
        previous_pressure = self.previous_pressure
        previous_received = self.previous_received
        state = observation.get("state") or {}
        metrics = state.get("metrics") or {}
        static_hold = metrics.get("static_hold_sec")

        if pressure is not None:
            self.previous_pressure = pressure
            self.previous_received = received
        if self.stage is None:
            if isinstance(static_hold, (int, float)):
                self.previous_static_hold = static_hold
            return None

        activity = activity_value(pressure, previous_pressure)
        deltas = (
            None
            if pressure is None or previous_pressure is None
            else tuple(abs(a - b) for a, b in zip(pressure, previous_pressure))
        )
        row = {
            "record_type": "sample",
            "timestamp": (
                chair_payload.get("t") if isinstance(chair_payload, dict) else None
            ),
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stage": self.stage,
            "stage_elapsed_sec": round(received - self.stage_started, 6),
            "sample_interval_sec": (
                None if previous_received is None else round(received - previous_received, 6)
            ),
            "valid_pressure": pressure is not None,
            "static_eps": STATIC_EPS,
            "fusion_state": state.get("state"),
            "score": state.get("score"),
            "fusion_seated": metrics.get("seated"),
            "static_hold_sec": static_hold,
            "static_hold_previous_sec": self.previous_static_hold,
            "static_hold_reset": (
                isinstance(static_hold, (int, float))
                and isinstance(self.previous_static_hold, (int, float))
                and static_hold < self.previous_static_hold
            ),
            "sensor_gap": (
                previous_received is not None
                and received - previous_received > self.gap_sec
            ),
        }
        if pressure is None:
            row["invalid_reason"] = "pressure_must_have_four_finite_numbers"
            row["observed_shape"] = pressure_shape(chair_payload, chair)
        else:
            row.update({name: value for name, value in zip(PRESSURE_NAMES, pressure)})
            row.update(
                {
                    "pressure_sum": sum(pressure),
                    "occupied_by_pressure": sum(pressure) >= OCCUPANCY_MIN,
                    "delta_fl": None if deltas is None else deltas[0],
                    "delta_fr": None if deltas is None else deltas[1],
                    "delta_bl": None if deltas is None else deltas[2],
                    "delta_br": None if deltas is None else deltas[3],
                    "activity": activity,
                    "activity_exceeds_static_eps": (
                        None if activity is None else activity > STATIC_EPS
                    ),
                }
            )
        if isinstance(static_hold, (int, float)):
            self.previous_static_hold = static_hold
        self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.sink.flush()
        return row


def load_rows(path):
    rows = []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL at line {line_number}") from error
            if row.get("record_type") == "sample":
                rows.append(row)
    return rows


def _stable_segments(rows, gap_sec=DEFAULT_GAP_SEC):
    segments, current = [], 0.0
    for row in rows:
        interval = row.get("sample_interval_sec")
        stable = (
            row.get("valid_pressure") is True
            and row.get("occupied_by_pressure") is True
            and row.get("activity") is not None
            and row.get("activity") <= STATIC_EPS
            and isinstance(interval, (int, float))
            and 0 < interval <= gap_sec
        )
        if stable:
            current += interval
        elif current:
            segments.append(current)
            current = 0.0
    if current:
        segments.append(current)
    return segments


def analyze_rows(rows):
    result = {"static_eps": STATIC_EPS, "stages": {}}
    labels = list(dict.fromkeys(row.get("stage") for row in rows if row.get("stage")))
    for label in labels:
        stage_rows = [row for row in rows if row.get("stage") == label]
        valid = [row for row in stage_rows if row.get("valid_pressure") is True]
        activities = [row["activity"] for row in valid if row.get("activity") is not None]
        intervals = [
            row["sample_interval_sec"]
            for row in valid
            if isinstance(row.get("sample_interval_sec"), (int, float))
        ]
        over_rows = [row for row in valid if row.get("activity_exceeds_static_eps") is True]
        result["stages"][label] = {
            "samples": len(stage_rows),
            "valid_samples": len(valid),
            "invalid_samples": len(stage_rows) - len(valid),
            "occupied_samples": sum(row.get("occupied_by_pressure") is True for row in valid),
            "sample_interval_sec": distribution(intervals),
            "sensor_gap_count": sum(row.get("sensor_gap") is True for row in stage_rows),
            "activity": distribution(activities),
            "activity_over_threshold_count": len(over_rows),
            "activity_over_threshold_ratio": (
                None if not activities else len(over_rows) / len(activities)
            ),
            "continuous_at_or_below_threshold_sec": distribution(_stable_segments(valid)),
            "channel_delta": {
                name: distribution(
                    [row[f"delta_{name}"] for row in valid if row.get(f"delta_{name}") is not None]
                )
                for name in PRESSURE_NAMES
            },
            "static_hold_reset_count": sum(row.get("static_hold_reset") is True for row in valid),
            "over_threshold_with_static_reset_count": sum(
                row.get("static_hold_reset") is True for row in over_rows
            ),
            "over_threshold_events": [
                {
                    "timestamp": row.get("timestamp"),
                    "activity": row.get("activity"),
                    "static_hold_sec": row.get("static_hold_sec"),
                    "static_hold_reset": row.get("static_hold_reset"),
                }
                for row in over_rows
            ],
        }
    return result


def write_summary(log_path, summary):
    summary_path = Path(log_path).with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def run_guided(recorder, *, record_sec, read=input, sleep=time.sleep, write=print):
    completed = []
    for label, instruction in STAGES:
        answer = read(
            f"\n[PREPARE {label}] {instruction}\n"
            "안정되면 Enter/Space를 누르세요. q 입력 시 종료: "
        )
        if answer.strip().lower() == "q":
            break
        recorder.start_stage(label)
        write(f"[RECORDING {label}] {record_sec:.0f}초")
        started = recorder.monotonic()
        try:
            while recorder.monotonic() - started < record_sec:
                sleep(min(0.1, record_sec))
        except KeyboardInterrupt:
            recorder.stop_stage()
            write("[chair-activity] 중단됨; 기록된 JSONL은 보존됩니다.")
            break
        recorder.stop_stage()
        completed.append(label)
    return completed


def build_parser():
    parser = argparse.ArgumentParser(description="R18 Chair 압력 활동량 진단")
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--analyze", type=Path, help="기존 진단 JSONL만 다시 분석")
    return parser


def default_output_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "logs" / f"chair_activity_{stamp}.jsonl"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analyze:
        summary = analyze_rows(load_rows(args.analyze))
        path = write_summary(args.analyze, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[chair-activity] summary={path}")
        return 0
    if not math.isfinite(args.record_sec) or args.record_sec <= 0:
        parser.error("--record-sec must be a positive finite number")

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    token = os.getenv(args.token_env)
    if not token:
        parser.error(f"환경변수 {args.token_env}에 Supabase access token이 필요합니다")
    try:
        import socketio
    except ImportError:
        parser.error("python-socketio가 설치되어 있지 않습니다")

    output = args.output or default_output_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    client = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
    try:
        with output.open("x", encoding="utf-8", buffering=1) as sink:
            recorder = ActivityRecorder(sink)
            client.on("cross_validation_observation", recorder.record)
            client.on(
                "measurement_phase",
                lambda payload: print(
                    f"[chair-activity] phase={payload.get('phase')} status={payload.get('status')}"
                ),
            )
            connect_authenticated_socket(client, args.url, token, observe_sensor_data=True)
            print("[chair-activity] 읽기 전용 Observer 연결 완료")
            print("[chair-activity] Front 연결과 기존 측정 세션을 유지하세요.")
            run_guided(recorder, record_sec=args.record_sec)
    except KeyboardInterrupt:
        print("[chair-activity] 중단됨; 기록된 JSONL은 보존됩니다.")
    except Exception as error:  # noqa: BLE001
        print(
            f"[chair-activity] 실패 ({type(error).__name__}); credential은 출력하지 않습니다.",
            file=sys.stderr,
        )
        return 1
    finally:
        if client.connected:
            client.disconnect()

    summary = analyze_rows(load_rows(output))
    summary_path = write_summary(output, summary)
    print(f"[chair-activity] log={output}")
    print(f"[chair-activity] summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
