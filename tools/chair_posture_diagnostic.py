#!/usr/bin/env python3
"""Read-only R19 Chair posture-distribution recorder and analyzer."""

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

from fusion.state import STATIC_EPS  # noqa: E402
from tools.chair_activity_diagnostic import (  # noqa: E402
    activity_value,
    chair_payload_parts,
    distribution,
    pressure_shape,
    pressure_values,
)
from tools.chair_e2e import connect_authenticated_socket  # noqa: E402


DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
DEFAULT_RECORD_SEC = 30.0
PRESSURE_NAMES = ("fl", "fr", "bl", "br")
STAGES = (
    ("CENTER", "의자 중앙에 자연스럽게 앉아 자세를 유지하세요."),
    ("LEFT", "왼쪽으로 체중을 이동한 뒤 안정적으로 유지하세요."),
    ("RIGHT", "오른쪽으로 체중을 이동한 뒤 안정적으로 유지하세요."),
    ("FORWARD", "상체를 앞으로 기울인 뒤 안정적으로 유지하세요."),
    ("BACK", "등받이에 기대어 안정적으로 유지하세요."),
    ("RETURN", "처음과 같은 중앙 착석 자세로 돌아와 유지하세요."),
)


def pressure_distribution(pressure):
    """Return normalized pressure features or an explicit invalid result."""
    total = sum(pressure)
    if not math.isfinite(total) or total <= 0:
        return {
            "valid": False,
            "reason": "pressure_sum_must_be_positive",
            "pressure_sum": total,
            "channel_share": None,
            "left_right_ratio": None,
            "front_back_ratio": None,
        }
    shares = tuple(value / total for value in pressure)
    fl, fr, bl, br = pressure
    return {
        "valid": True,
        "reason": None,
        "pressure_sum": total,
        "channel_share": dict(zip(PRESSURE_NAMES, shares)),
        "left_right_ratio": ((fl + bl) - (fr + br)) / total,
        "front_back_ratio": ((fl + fr) - (bl + br)) / total,
    }


class PostureRecorder:
    """Write only confirmed RECORDING observations to line-buffered JSONL."""

    def __init__(self, sink, *, monotonic=time.monotonic):
        self.sink = sink
        self.monotonic = monotonic
        self.stage = None
        self.stage_started = None
        self.previous_pressure = None

    def start_stage(self, label):
        self.stage = label
        self.stage_started = self.monotonic()
        self.previous_pressure = None

    def stop_stage(self):
        self.stage = None
        self.stage_started = None
        self.previous_pressure = None

    def record(self, observation):
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
                chair_payload.get("t") if isinstance(chair_payload, dict) else None
            ),
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stage": self.stage,
            "stage_elapsed_sec": round(received - self.stage_started, 6),
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
            activity = activity_value(pressure, self.previous_pressure)
            row.update(dict(zip(PRESSURE_NAMES, pressure)))
            row.update(
                {
                    "pressure_sum": features["pressure_sum"],
                    "channel_share": features["channel_share"],
                    "left_right_ratio": features["left_right_ratio"],
                    "front_back_ratio": features["front_back_ratio"],
                    "valid_distribution": features["valid"],
                    "distribution_invalid_reason": features["reason"],
                    "activity": activity,
                }
            )
            self.previous_pressure = pressure
        self.sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.sink.flush()
        return row


def _median(values):
    return None if not values else statistics.median(values)


def _feature_vector(row):
    shares = row.get("channel_share")
    if not isinstance(shares, dict):
        return None
    values = tuple(shares.get(name) for name in PRESSURE_NAMES)
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        return None
    return values


def _distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _stage_statistics(rows):
    valid = [row for row in rows if row.get("valid_pressure") is True]
    distributed = [row for row in valid if row.get("valid_distribution") is True]
    vectors = [_feature_vector(row) for row in distributed]
    vectors = [vector for vector in vectors if vector is not None]
    centroid = (
        None
        if not vectors
        else tuple(statistics.median(vector[index] for vector in vectors) for index in range(4))
    )
    radii = [] if centroid is None else [_distance(vector, centroid) for vector in vectors]
    activity = [row["activity"] for row in valid if row.get("activity") is not None]
    left_right = [row["left_right_ratio"] for row in distributed]
    front_back = [row["front_back_ratio"] for row in distributed]
    channel_share = {
        name: _median([row["channel_share"][name] for row in distributed])
        for name in PRESSURE_NAMES
    }
    activity_stats = distribution(activity)
    left_stats = distribution(left_right)
    front_stats = distribution(front_back)
    stable_count = sum(value <= STATIC_EPS for value in activity)
    return {
        "samples": len(rows),
        "valid_samples": len(valid),
        "invalid_samples": len(rows) - len(valid),
        "valid_distribution_samples": len(distributed),
        "pressure_median": {
            name: _median([row[name] for row in valid if name in row])
            for name in PRESSURE_NAMES
        },
        "pressure_sum_median": _median([row["pressure_sum"] for row in valid]),
        "left_right_ratio": {
            **left_stats,
            "p95_minus_p05": (
                None
                if not left_right
                else _percentile(left_right, 95) - _percentile(left_right, 5)
            ),
        },
        "front_back_ratio": {
            **front_stats,
            "p95_minus_p05": (
                None
                if not front_back
                else _percentile(front_back, 95) - _percentile(front_back, 5)
            ),
        },
        "channel_share_median": channel_share,
        "activity": {
            "median": activity_stats["median"],
            "p95": activity_stats["p95"],
            "max": activity_stats["max"],
        },
        "stability": {
            "activity_at_or_below_static_eps_ratio": (
                None if not activity else stable_count / len(activity)
            ),
            "feature_distance_from_median": distribution(radii),
        },
        "_feature_centroid": centroid,
        "_feature_radius_p95": None if not radii else _percentile(radii, 95),
    }


def _percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _comparison(reference_label, target_label, stats):
    reference = stats.get(reference_label)
    target = stats.get(target_label)
    if not reference or not target:
        return {"available": False, "reason": "stage_missing"}
    first = reference["_feature_centroid"]
    second = target["_feature_centroid"]
    if first is None or second is None:
        return {"available": False, "reason": "valid_distribution_missing"}
    distance = _distance(first, second)
    first_radius = reference["_feature_radius_p95"] or 0.0
    second_radius = target["_feature_radius_p95"] or 0.0
    combined = first_radius + second_radius
    return {
        "available": True,
        "channel_share_centroid_distance": distance,
        "left_right_ratio_median_delta": (
            target["left_right_ratio"]["median"]
            - reference["left_right_ratio"]["median"]
        ),
        "front_back_ratio_median_delta": (
            target["front_back_ratio"]["median"]
            - reference["front_back_ratio"]["median"]
        ),
        "reference_internal_radius_p95": first_radius,
        "target_internal_radius_p95": second_radius,
        "combined_internal_radius_p95": combined,
        "distance_to_combined_internal_variation": (
            None if combined == 0 else distance / combined
        ),
        "robust_p95_regions_overlap": distance <= combined,
    }


def analyze_rows(rows):
    stage_stats = {
        label: _stage_statistics([row for row in rows if row.get("stage") == label])
        for label, _instruction in STAGES
    }
    comparisons = {
        label: _comparison("CENTER", label, stage_stats)
        for label in ("LEFT", "RIGHT", "FORWARD", "BACK")
    }
    comparisons["RETURN"] = _comparison("CENTER", "RETURN", stage_stats)
    for stats in stage_stats.values():
        stats.pop("_feature_centroid")
        stats.pop("_feature_radius_p95")
    return {
        "v": 1,
        "comparison_feature": "normalized_channel_share",
        "variability_definition": "p95_minus_p05",
        "stages": stage_stats,
        "center_comparisons": comparisons,
        "center_return_similarity": comparisons["RETURN"],
        "classification_thresholds_selected": False,
    }


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
            "자세가 안정되면 Enter를 누르세요. q 입력 시 종료: "
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
            write("[chair-posture] 중단됨; 기록된 JSONL은 보존됩니다.")
            break
        recorder.stop_stage()
        completed.append(label)
    return completed


def build_parser():
    parser = argparse.ArgumentParser(description="R19 Chair 자세별 압력 분포 진단")
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--analyze", type=Path)
    return parser


def default_output_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "logs" / f"chair_posture_{stamp}.jsonl"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analyze:
        summary = analyze_rows(load_rows(args.analyze))
        path = write_summary(args.analyze, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[chair-posture] summary={path}")
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
            recorder = PostureRecorder(sink)
            client.on("cross_validation_observation", recorder.record)
            connect_authenticated_socket(client, args.url, token, observe_sensor_data=True)
            print("[chair-posture] 읽기 전용 Observer 연결 완료")
            print("[chair-posture] Front 연결과 기존 MEASURING 세션을 유지하세요.")
            run_guided(recorder, record_sec=args.record_sec)
    except KeyboardInterrupt:
        print("[chair-posture] 중단됨; 기록된 JSONL은 보존됩니다.")
    except Exception as error:  # noqa: BLE001
        print(
            f"[chair-posture] 실패 ({type(error).__name__}); credential은 출력하지 않습니다.",
            file=sys.stderr,
        )
        return 1
    finally:
        if client.connected:
            client.disconnect()

    summary_path = write_summary(output, analyze_rows(load_rows(output)))
    print(f"[chair-posture] log={output}")
    print(f"[chair-posture] summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
