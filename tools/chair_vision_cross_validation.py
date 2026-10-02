#!/usr/bin/env python3
"""Guided Chair/Vision observational cross-validation recorder.

This development tool subscribes to the Backend's opt-in
``cross_validation_observation`` event.  It does not classify a combined
posture and never feeds observations back into Fusion, SOMA Load, or Feedback.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chair_e2e import (  # noqa: E402
    ChairE2EError,
    connect_authenticated_socket,
    post_measurement,
)
from tools.access_token import (  # noqa: E402
    AccessTokenLifetimeError,
    require_access_token_lifetime,
)


DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
DEFAULT_RECORD_SEC = 5.0
DEFAULT_OUTPUT_DIR = ROOT / "logs"
FRESH_VISION = frozenset({"FRESH", "RECOVERED"})
PREPARE_ALLOWANCE_PER_STAGE_SEC = 15.0
SHUTDOWN_SAFETY_MARGIN_SEC = 60.0

STATE_PREPARE = "PREPARE"
STATE_RECORDING = "RECORDING"
STATE_COMPLETE = "COMPLETE"
STATE_ABORTED = "ABORTED"


@dataclass(frozen=True)
class Scenario:
    label: str
    instruction: str
    expected_chair: str
    expected_vision: str


SCENARIOS = (
    Scenario("CENTER", "평소처럼 중앙에 앉아 화면을 정면으로 보세요.", "CENTER", "CENTER"),
    Scenario(
        "WEIGHT_LEFT",
        "앉은 상태에서 몸의 하중을 사용자 기준 왼쪽으로 분명히 옮기세요.",
        "LEFT",
        "ANY",
    ),
    Scenario("CENTER", "중앙 착석으로 돌아오세요.", "CENTER", "CENTER"),
    Scenario(
        "WEIGHT_RIGHT",
        "앉은 상태에서 몸의 하중을 사용자 기준 오른쪽으로 분명히 옮기세요.",
        "RIGHT",
        "ANY",
    ),
    Scenario("CENTER", "중앙 착석으로 돌아오세요.", "CENTER", "CENTER"),
    Scenario(
        "UPPER_BODY_LEFT",
        "엉덩이는 평소 착석 위치에 가깝게 두고 상체를 사용자 기준 왼쪽으로 자연스럽게 기울이세요.",
        "ANY",
        "LEFT",
    ),
    Scenario("CENTER", "중앙 착석으로 돌아오세요.", "CENTER", "CENTER"),
    Scenario(
        "UPPER_BODY_RIGHT",
        "엉덩이는 평소 착석 위치에 가깝게 두고 상체를 사용자 기준 오른쪽으로 자연스럽게 기울이세요.",
        "ANY",
        "RIGHT",
    ),
    Scenario("CENTER", "중앙 착석으로 돌아오세요.", "CENTER", "CENTER"),
    Scenario("HEAD_TURN_LEFT", "몸과 좌석은 중앙에 두고 얼굴만 왼쪽을 보세요.", "CENTER", "CENTER_OR_UNKNOWN"),
    Scenario("CENTER", "얼굴과 몸을 중앙으로 돌리세요.", "CENTER", "CENTER"),
    Scenario("HEAD_TURN_RIGHT", "몸과 좌석은 중앙에 두고 얼굴만 오른쪽을 보세요.", "CENTER", "CENTER_OR_UNKNOWN"),
    Scenario("CENTER", "얼굴과 몸을 중앙으로 돌리세요.", "CENTER", "CENTER"),
    Scenario("STAND_UP", "의자에서 완전히 일어나 좌석에서 체중을 모두 빼세요.", "ABSENT", "ANY"),
    Scenario("CENTER", "다시 평소처럼 중앙에 앉으세요.", "CENTER", "CENTER"),
)


class GuidedExperiment:
    """PREPARE -> explicit confirmation -> timed RECORDING workflow."""

    def __init__(self, scenarios=SCENARIOS, record_sec=DEFAULT_RECORD_SEC):
        if record_sec <= 0:
            raise ValueError("record_sec must be positive")
        if not scenarios:
            raise ValueError("at least one scenario is required")
        self.scenarios = tuple(scenarios)
        self.record_sec = float(record_sec)
        self.stage_index = 0
        self.state = STATE_PREPARE
        self.recording_started_at = None

    @property
    def scenario(self):
        if self.state in (STATE_COMPLETE, STATE_ABORTED):
            return None
        return self.scenarios[self.stage_index]

    @property
    def is_recording(self):
        return self.state == STATE_RECORDING

    def confirm(self, now):
        if self.state != STATE_PREPARE:
            return False
        self.state = STATE_RECORDING
        self.recording_started_at = float(now)
        return True

    def advance(self, now):
        if not self.is_recording:
            return False
        if float(now) - self.recording_started_at < self.record_sec:
            return False
        self.stage_index += 1
        self.recording_started_at = None
        self.state = (
            STATE_COMPLETE
            if self.stage_index == len(self.scenarios)
            else STATE_PREPARE
        )
        return True

    def abort(self):
        if self.state == STATE_COMPLETE:
            return False
        self.state = STATE_ABORTED
        self.recording_started_at = None
        return True


class JsonlSink:
    """Line-buffered append-only output that remains valid after interruption."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.path.open("x", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, row):
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._output.write(encoded + "\n")
            self._output.flush()

    def close(self):
        with self._lock:
            if not self._output.closed:
                self._output.close()


def utc_timestamp(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def default_output_path(now=None):
    stamp = time.strftime(
        "%Y%m%dT%H%M%SZ",
        time.gmtime(time.time() if now is None else now),
    )
    return DEFAULT_OUTPUT_DIR / f"chair_vision_cross_validation_{stamp}.jsonl"


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_row(observation, *, scenario, stage_index, experiment_started_at,
              received_at, monotonic_now, scenario_started_at):
    """Convert one Chair-authoritative observation into a JSONL row."""
    chair_payload = observation["chair"]
    pressure = list(chair_payload["chair"]["pressure"])
    if len(pressure) != 4:
        raise ValueError("Chair pressure must contain FL/FR/BL/BR")
    decision = observation["state"]
    metrics = decision.get("metrics", {})
    seated = bool(metrics.get("seated", sum(pressure) >= 100))

    availability = str(observation.get("vision_availability", "UNSEEN"))
    vision_fresh = availability in FRESH_VISION
    vision_payload = observation.get("vision") or {}
    vision = vision_payload.get("vision", {}) if vision_fresh else {}
    direction = vision.get("face_lean_direction", "UNKNOWN")
    if direction not in {"CENTER", "LEFT", "RIGHT", "UNKNOWN"}:
        direction = "UNKNOWN"

    chair_t = float(chair_payload["t"])
    vision_t = (
        _finite_or_none(vision_payload.get("t"))
        if observation.get("vision") is not None else None
    )
    return {
        "v": 1,
        "timestamp": utc_timestamp(chair_t),
        "recorded_at": utc_timestamp(received_at),
        "elapsed_sec": round(float(monotonic_now) - experiment_started_at, 3),
        "scenario_elapsed_sec": round(
            float(monotonic_now) - scenario_started_at,
            3,
        ),
        "scenario_label": scenario.label,
        "stage_index": int(stage_index) + 1,
        "expected": {
            "chair": scenario.expected_chair,
            "vision": scenario.expected_vision,
        },
        "chair": {
            "sender_t": chair_t,
            "fl": pressure[0],
            "fr": pressure[1],
            "bl": pressure[2],
            "br": pressure[3],
            "ir": list(chair_payload["chair"].get("ir", [])),
            "pressure_sum": sum(pressure),
            "balance": metrics.get("balance", "CENTER"),
            "static_hold_sec": metrics.get("static_hold_sec"),
            "seated": seated,
            "occupancy": "PRESENT" if seated else "ABSENT",
        },
        "vision": {
            "sender_t": vision_t,
            "availability": availability,
            "fresh": vision_fresh,
            "receipt_age_sec": observation.get("vision_receipt_age_sec"),
            "chair_sender_delta_sec": observation.get(
                "chair_vision_sender_delta_sec"
            ),
            "face_lean_direction": direction,
            "face_lateral_offset": vision.get("face_lateral_offset"),
            "head_roll_delta_deg": vision.get("head_roll_delta_deg"),
            "face_detected": vision.get("face_detected"),
            "detect_rate": vision.get("detect_rate"),
            "yaw_dropped_rate": vision.get("yaw_dropped_rate"),
        },
        "fusion": {
            "state": decision.get("state"),
            "score": decision.get("score"),
            "confidence": decision.get("confidence"),
        },
    }


class ObservationRecorder:
    """Thread-safe bridge from Socket.IO callbacks to the guided JSONL sink."""

    def __init__(self, workflow, sink, *, monotonic=time.monotonic,
                 wall_time=time.time):
        self.workflow = workflow
        self.sink = sink
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.started_at = monotonic()
        self.rows = []
        self._lock = threading.Lock()

    def confirm(self, now):
        with self._lock:
            return self.workflow.confirm(now)

    def advance(self, now):
        with self._lock:
            return self.workflow.advance(now)

    def abort(self):
        with self._lock:
            return self.workflow.abort()

    def record(self, observation):
        with self._lock:
            now = self.monotonic()
            if not self.workflow.is_recording:
                return None
            if (
                now - self.workflow.recording_started_at
                >= self.workflow.record_sec
            ):
                self.workflow.advance(now)
                return None
            row = build_row(
                observation,
                scenario=self.workflow.scenario,
                stage_index=self.workflow.stage_index,
                experiment_started_at=self.started_at,
                received_at=self.wall_time(),
                monotonic_now=now,
                scenario_started_at=self.workflow.recording_started_at,
            )
            self.sink.write(row)
            self.rows.append(row)
            return row


def _percentages(counter):
    total = sum(counter.values())
    if total == 0:
        return []
    return [
        (name, count, 100.0 * count / total)
        for name, count in counter.most_common()
    ]


def summarize_rows(rows):
    """Aggregate each scenario without assuming Chair and Vision must agree."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["scenario_label"]].append(row)

    result = {}
    for label, samples in grouped.items():
        chair = Counter(
            "ABSENT" if row["chair"]["occupancy"] == "ABSENT"
            else row["chair"]["balance"]
            for row in samples
        )
        vision = Counter(
            row["vision"]["face_lean_direction"]
            if row["vision"]["fresh"] else "UNAVAILABLE"
            for row in samples
        )
        offsets = [
            row["vision"]["face_lateral_offset"]
            for row in samples
            if row["vision"]["fresh"]
            and row["vision"]["face_lateral_offset"] is not None
        ]
        result[label] = {
            "expected": samples[0]["expected"],
            "samples": len(samples),
            "chair": _percentages(chair),
            "vision": _percentages(vision),
            "median_face_lateral_offset": (
                None if not offsets else statistics.median(offsets)
            ),
        }
    return result


def print_summary(rows, write=print):
    write("\n[cross-validation] scenario summary")
    summary = summarize_rows(rows)
    for scenario in SCENARIOS:
        if scenario.label not in summary:
            continue
        item = summary.pop(scenario.label)
        write(
            f"\n{scenario.label} "
            f"(expected Chair={item['expected']['chair']}, "
            f"Vision={item['expected']['vision']}, n={item['samples']})"
        )
        write("  Chair: " + ", ".join(
            f"{name} {percent:.1f}%" for name, _count, percent in item["chair"]
        ))
        write("  Vision: " + ", ".join(
            f"{name} {percent:.1f}%" for name, _count, percent in item["vision"]
        ))
        median = item["median_face_lateral_offset"]
        write(
            "  median lateral offset: "
            + ("NA" if median is None else f"{median:+.4f}")
        )


def print_prepare(workflow, write=print):
    scenario = workflow.scenario
    write(
        f"\n[PREPARE {workflow.stage_index + 1}/{len(workflow.scenarios)}] "
        f"{scenario.label}"
    )
    write(f"  자세: {scenario.instruction}")
    write(
        f"  expected observation: Chair={scenario.expected_chair}, "
        f"Vision={scenario.expected_vision}"
    )
    write("  자세가 안정되면 Enter 또는 Space를 누르세요. q는 중단합니다.")


def read_confirmation():
    """Accept SPACE or ENTER immediately on Windows; use a line elsewhere."""
    if os.name == "nt" and sys.stdin.isatty():
        import msvcrt

        while True:
            key = msvcrt.getwch()
            if key in (" ", "\r", "\n"):
                print()
                return ""
            if key.lower() == "q":
                print("q")
                return "q"
    return input()


def run_guided(workflow, recorder, *, read=None, write=print,
               monotonic=time.monotonic, sleeper=time.sleep):
    read = read or read_confirmation
    while workflow.state not in (STATE_COMPLETE, STATE_ABORTED):
        print_prepare(workflow, write)
        answer = read().strip().lower()
        if answer == "q":
            workflow.abort()
            break
        if answer:
            write("  Enter 또는 Space+Enter만 사용하세요.")
            continue
        started = monotonic()
        recorder.confirm(started)
        write(f"[RECORDING] {workflow.scenario.label} — {workflow.record_sec:.1f}초")
        while workflow.is_recording:
            now = monotonic()
            if recorder.advance(now):
                break
            sleeper(min(0.1, max(workflow.record_sec - (now - started), 0.01)))
    return workflow.state


def build_parser():
    parser = argparse.ArgumentParser(
        description="Chair/Vision controlled cross-validation JSONL recorder"
    )
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}",
    )
    parser.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Supabase access token이 들어 있는 환경변수 이름",
    )
    return parser


def connect_observation_client(client, base_url, token):
    """Connect an authenticated, opt-in observer over HTTP long-polling."""
    connect_authenticated_socket(
        client,
        base_url,
        token,
        observe_sensor_data=True,
    )


def cross_validation_required_lifetime(record_sec, *, stage_count=None):
    """Estimate a conservative minimum despite user-controlled PREPARE time."""
    record_sec = float(record_sec)
    stage_count = len(SCENARIOS) if stage_count is None else int(stage_count)
    if record_sec <= 0 or stage_count <= 0:
        raise ValueError("record_sec and stage_count must be positive")
    return (
        stage_count * (record_sec + PREPARE_ALLOWANCE_PER_STAGE_SEC)
        + SHUTDOWN_SAFETY_MARGIN_SEC
    )


def report_run_result(
    path,
    row_count,
    workflow_state,
    stop_error,
    *,
    write=print,
    write_error=None,
):
    """Report protocol data and shutdown authentication as separate results."""
    write_error = write_error or write
    protocol_result = (
        "COMPLETE" if workflow_state == STATE_COMPLETE else "ABORTED"
    )
    write(
        f"\n[cross-validation] protocol result: {protocol_result} "
        f"({row_count} rows)"
    )
    write(f"[cross-validation] 저장 완료: {path} ({row_count} rows)")
    if stop_error is None:
        write("[cross-validation] shutdown HTTP result: SUCCESS")
        return 0
    write_error(
        "[cross-validation] shutdown HTTP result: FAILED; "
        "recording data remains valid and Socket.IO disconnect cleanup was "
        f"attempted ({stop_error})"
    )
    return 1


def main(argv=None, *, token_now=time.time):
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.record_sec <= 0:
        parser.error("--record-sec은 0보다 커야 합니다")
    token = os.getenv(args.token_env)
    if not token:
        parser.error(f"환경변수 {args.token_env}에 Supabase access token이 필요합니다")
    required_lifetime = cross_validation_required_lifetime(args.record_sec)
    try:
        require_access_token_lifetime(
            token,
            required_lifetime,
            now=token_now,
        )
    except AccessTokenLifetimeError as error:
        parser.error(str(error))

    try:
        import socketio
    except ImportError:
        sys.exit("python-socketio가 없습니다: pip install -r tools/requirements.txt")

    path = args.output or default_output_path()
    try:
        sink = JsonlSink(path)
    except FileExistsError:
        parser.error(f"출력 파일이 이미 존재합니다: {path}")
    workflow = GuidedExperiment(record_sec=args.record_sec)
    recorder = ObservationRecorder(workflow, sink)
    client = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
    client.on("cross_validation_observation", recorder.record)
    connected = started = False
    stop_error = None
    try:
        connect_observation_client(client, args.url, token)
        connected = True
        post_measurement(args.url, "/api/measurement/start", token)
        started = True
        print(f"[cross-validation] 저장 위치: {path}")
        print("[cross-validation] PREPARE 중에는 기록하지 않습니다.")
        run_guided(workflow, recorder)
    except KeyboardInterrupt:
        recorder.abort()
        print("\n[cross-validation] 사용자 중단 — 기록된 JSONL은 보존합니다.")
    except ChairE2EError as error:
        print(f"[cross-validation] {error}", file=sys.stderr)
        return 1
    finally:
        recorder.abort()
        if started:
            try:
                post_measurement(args.url, "/api/measurement/stop", token)
            except ChairE2EError as error:
                stop_error = error
        if connected and client.connected:
            client.disconnect()
        sink.close()

    print_summary(recorder.rows)
    return report_run_result(
        path,
        len(recorder.rows),
        workflow.state,
        stop_error,
        write_error=lambda message: print(message, file=sys.stderr),
    )


if __name__ == "__main__":
    raise SystemExit(main())
