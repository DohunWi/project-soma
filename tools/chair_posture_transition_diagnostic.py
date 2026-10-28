#!/usr/bin/env python3
"""Read-only R19 continuous Chair posture-transition recorder."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.state import STATIC_EPS  # noqa: E402
from tools.chair_activity_diagnostic import (  # noqa: E402
    activity_value,
    chair_payload_parts,
    distribution,
    pressure_shape,
    pressure_values,
)
from tools.chair_e2e import connect_authenticated_socket  # noqa: E402
from tools.chair_posture_diagnostic import pressure_distribution  # noqa: E402


DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
DEFAULT_RECORD_SEC = 30.0
PRESSURE_NAMES = ("fl", "fr", "bl", "br")
STAGES = (
    ("CENTER", "중앙 착석 자세를 유지하세요."),
    ("LEFT", "지금 왼쪽으로 체중을 이동한 뒤 유지하세요."),
    ("CENTER_RETURN_1", "지금 중앙 착석 자세로 복귀해 유지하세요."),
    ("RIGHT", "지금 오른쪽으로 체중을 이동한 뒤 유지하세요."),
    ("CENTER_RETURN_2", "지금 중앙 착석 자세로 복귀해 유지하세요."),
)
STABILITY_CONSECUTIVE_SAMPLES = 3
STABILITY_TAIL_FRACTION = 0.5


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TransitionRecorder:
    """Record stage transitions and samples without resetting pressure history."""

    def __init__(self, sink, *, monotonic=time.monotonic):
        self.sink = sink
        self.monotonic = monotonic
        self.stage = None
        self.stage_started = None
        self.experiment_started = None
        self.previous_pressure = None
        self._lock = threading.Lock()

    def begin(self, label, instruction):
        with self._lock:
            now = self.monotonic()
            self.experiment_started = now
            self._write_transition(None, label, instruction, now)
            self.stage = label
            self.stage_started = now

    def transition(self, label, instruction):
        with self._lock:
            now = self.monotonic()
            previous = self.stage
            self._write_transition(previous, label, instruction, now)
            self.stage = label
            self.stage_started = now

    def stop(self):
        with self._lock:
            self.stage = None
            self.stage_started = None

    def _write_transition(self, previous, current, instruction, now):
        row = {
            "record_type": "stage_transition",
            "recorded_at": utc_now_iso(),
            "from_stage": previous,
            "to_stage": current,
            "transition_instruction": instruction,
            "experiment_elapsed_sec": (
                0.0
                if self.experiment_started is None
                else round(now - self.experiment_started, 6)
            ),
        }
        self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.sink.flush()

    def record(self, observation):
        with self._lock:
            if self.stage is None:
                return None
            received = self.monotonic()
            chair_payload, chair = chair_payload_parts(observation)
            pressure = pressure_values(chair)
            state = observation.get("state") or {}
            metrics = state.get("metrics") or {}
            row = {
                "record_type": "sample",
                "timestamp": (
                    chair_payload.get("t")
                    if isinstance(chair_payload, dict)
                    else None
                ),
                "recorded_at": utc_now_iso(),
                "stage": self.stage,
                "stage_elapsed_sec": round(received - self.stage_started, 6),
                "experiment_elapsed_sec": round(
                    received - self.experiment_started,
                    6,
                ),
                "valid_pressure": pressure is not None,
                "fusion_state": state.get("state"),
                "fusion_seated": metrics.get("seated"),
                "static_hold_sec": metrics.get("static_hold_sec"),
            }
            if pressure is None:
                row["invalid_reason"] = "pressure_must_have_four_finite_numbers"
                row["observed_shape"] = pressure_shape(chair_payload, chair)
            else:
                features = pressure_distribution(pressure)
                row.update(dict(zip(PRESSURE_NAMES, pressure)))
                row.update(
                    {
                        "pressure_sum": features["pressure_sum"],
                        "channel_share": features["channel_share"],
                        "left_right_ratio": features["left_right_ratio"],
                        "front_back_ratio": features["front_back_ratio"],
                        "valid_distribution": features["valid"],
                        "distribution_invalid_reason": features["reason"],
                        "activity": activity_value(
                            pressure,
                            self.previous_pressure,
                        ),
                    }
                )
                self.previous_pressure = pressure
            self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.sink.flush()
            return row


def _feature_vector(row):
    shares = row.get("channel_share")
    if not isinstance(shares, dict):
        return None
    vector = tuple(shares.get(name) for name in PRESSURE_NAMES)
    if any(
        not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in vector
    ):
        return None
    return vector


def _distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _median_vector(rows):
    vectors = [_feature_vector(row) for row in rows]
    vectors = [vector for vector in vectors if vector is not None]
    if not vectors:
        return None
    return tuple(
        statistics.median(vector[index] for vector in vectors)
        for index in range(4)
    )


def _percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _stage_stats(rows, center):
    valid = [row for row in rows if row.get("valid_pressure") is True]
    distributed = [
        row for row in valid
        if row.get("valid_distribution") is True and _feature_vector(row) is not None
    ]
    activity = [row["activity"] for row in valid if row.get("activity") is not None]
    distances = (
        []
        if center is None
        else [_distance(_feature_vector(row), center) for row in distributed]
    )
    return {
        "samples": len(rows),
        "valid_samples": len(valid),
        "invalid_samples": len(rows) - len(valid),
        "valid_distribution_samples": len(distributed),
        "pressure_median": {
            name: (
                None
                if not valid
                else statistics.median(row[name] for row in valid if name in row)
            )
            for name in PRESSURE_NAMES
        },
        "pressure_sum_median": (
            None
            if not valid
            else statistics.median(row["pressure_sum"] for row in valid)
        ),
        "left_right_ratio": distribution(
            [row["left_right_ratio"] for row in distributed]
        ),
        "front_back_ratio": distribution(
            [row["front_back_ratio"] for row in distributed]
        ),
        "activity": {
            "median": distribution(activity)["median"],
            "p95": distribution(activity)["p95"],
            "max": distribution(activity)["max"],
            "over_static_eps_count": sum(value > STATIC_EPS for value in activity),
        },
        "distance_from_initial_center": distribution(distances),
    }


def _stabilization_estimate(stage_rows):
    distributed = [
        row for row in stage_rows
        if row.get("valid_distribution") is True and _feature_vector(row) is not None
    ]
    if len(distributed) < STABILITY_CONSECUTIVE_SAMPLES:
        return {"available": False, "reason": "insufficient_valid_samples"}
    tail_start = max(
        int(len(distributed) * (1.0 - STABILITY_TAIL_FRACTION)),
        0,
    )
    tail = distributed[tail_start:]
    target = _median_vector(tail)
    tail_distances = [_distance(_feature_vector(row), target) for row in tail]
    radius = _percentile(tail_distances, 95)
    epsilon = 1e-12
    allowed = max(radius, epsilon)
    distances = [_distance(_feature_vector(row), target) for row in distributed]
    first_index = None
    for index in range(len(distributed) - STABILITY_CONSECUTIVE_SAMPLES + 1):
        window = distances[index:index + STABILITY_CONSECUTIVE_SAMPLES]
        if all(distance <= allowed for distance in window):
            first_index = index
            break
    return {
        "available": first_index is not None,
        "method": "first_3_consecutive_samples_within_tail_half_p95_radius",
        "production_threshold": False,
        "tail_sample_count": len(tail),
        "tail_internal_radius_p95": radius,
        "estimated_sec_after_instruction": (
            None
            if first_index is None
            else distributed[first_index].get("stage_elapsed_sec")
        ),
        "first_stable_timestamp": (
            None if first_index is None else distributed[first_index].get("timestamp")
        ),
    }


def analyze_rows(rows):
    samples = [row for row in rows if row.get("record_type") == "sample"]
    transitions = [
        row for row in rows if row.get("record_type") == "stage_transition"
    ]
    center_rows = [
        row for row in samples
        if row.get("stage") == "CENTER" and row.get("valid_distribution") is True
    ]
    center = _median_vector(center_rows)
    timeline = []
    for row in samples:
        vector = _feature_vector(row)
        timeline.append(
            {
                "timestamp": row.get("timestamp"),
                "experiment_elapsed_sec": row.get("experiment_elapsed_sec"),
                "stage": row.get("stage"),
                "activity": row.get("activity"),
                "distance_from_initial_center": (
                    None
                    if center is None or vector is None
                    else _distance(vector, center)
                ),
            }
        )
    stage_stats = {}
    stabilization = {}
    for label, _instruction in STAGES:
        selected = [row for row in samples if row.get("stage") == label]
        stage_stats[label] = _stage_stats(selected, center)
        stabilization[label] = _stabilization_estimate(selected)
    spikes = [
        {
            **entry,
            "next_3_distance_median": _next_distance_median(timeline, index, 3),
        }
        for index, entry in enumerate(timeline)
        if isinstance(entry.get("activity"), (int, float))
        and entry["activity"] > STATIC_EPS
    ]
    return {
        "v": 1,
        "reference": "initial_CENTER_channel_share_median",
        "classification_thresholds_selected": False,
        "transition_events": transitions,
        "stages": stage_stats,
        "distance_timeline": timeline,
        "activity_spikes_over_existing_static_eps": spikes,
        "technical_stabilization_estimates": stabilization,
        "center_return_distance": {
            label: stage_stats[label]["distance_from_initial_center"]
            for label in ("CENTER_RETURN_1", "CENTER_RETURN_2")
        },
    }


def _next_distance_median(timeline, index, count):
    values = [
        entry.get("distance_from_initial_center")
        for entry in timeline[index:index + count]
        if isinstance(entry.get("distance_from_initial_center"), (int, float))
    ]
    return None if not values else statistics.median(values)


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


def write_summary(log_path, summary):
    summary_path = Path(log_path).with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def run_continuous(
    recorder,
    *,
    record_sec,
    read=input,
    sleep=time.sleep,
    write=print,
):
    first_label, first_instruction = STAGES[0]
    answer = read(
        f"\n[PREPARE {first_label}] {first_instruction}\n"
        "자세가 안정되면 Enter를 누르세요. q 입력 시 종료: "
    )
    if answer.strip().lower() == "q":
        return []
    recorder.begin(first_label, first_instruction)
    completed = []
    for index, (label, _instruction) in enumerate(STAGES):
        write(f"[RECORDING {label}] {record_sec:.0f}초")
        started = recorder.monotonic()
        try:
            while recorder.monotonic() - started < record_sec:
                sleep(min(0.1, record_sec))
        except KeyboardInterrupt:
            write("[chair-transition] 중단됨; 기록된 JSONL은 보존됩니다.")
            break
        completed.append(label)
        if index + 1 < len(STAGES):
            next_label, instruction = STAGES[index + 1]
            recorder.transition(next_label, instruction)
            write(f"[TRANSITION → {next_label}] {instruction}")
    recorder.stop()
    return completed


def build_parser():
    parser = argparse.ArgumentParser(description="R19 연속 Chair 자세 전환 진단")
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--analyze", type=Path)
    return parser


def default_output_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "logs" / f"chair_posture_transition_{stamp}.jsonl"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analyze:
        summary = analyze_rows(load_rows(args.analyze))
        path = write_summary(args.analyze, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[chair-transition] summary={path}")
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
            recorder = TransitionRecorder(sink)
            client.on("cross_validation_observation", recorder.record)
            connect_authenticated_socket(client, args.url, token, observe_sensor_data=True)
            print("[chair-transition] 읽기 전용 Observer 연결 완료")
            print("[chair-transition] 첫 Enter 이후 5단계를 연속 기록합니다.")
            run_continuous(recorder, record_sec=args.record_sec)
    except KeyboardInterrupt:
        print("[chair-transition] 중단됨; 기록된 JSONL은 보존됩니다.")
    except Exception as error:  # noqa: BLE001
        print(
            f"[chair-transition] 실패 ({type(error).__name__}); credential은 출력하지 않습니다.",
            file=sys.stderr,
        )
        return 1
    finally:
        if client.connected:
            client.disconnect()
    summary_path = write_summary(output, analyze_rows(load_rows(output)))
    print(f"[chair-transition] log={output}")
    print(f"[chair-transition] summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
