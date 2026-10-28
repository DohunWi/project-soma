#!/usr/bin/env python3
"""Record guided, observational face-lateral and head-roll JSONL data."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))

try:
    import cv2
except ImportError:
    sys.exit("opencv 가 없습니다. pip install -r vision/requirements.txt")

from calibrator import Calibrator, calibration_sample  # noqa: E402
from ear import face_ear_values  # noqa: E402
from geometry import face_width_px, is_frontal  # noqa: E402
from landmarks import FaceLandmarks  # noqa: E402
from lateral import (  # noqa: E402
    FaceLeanClassifier,
    angle_delta_deg,
    face_lateral_geometry,
    lateral_offset,
)

OUT_DIR = Path(__file__).parent / "data"

LABEL_NEUTRAL = "NEUTRAL_CENTER"
LABEL_USER_LEFT = "USER_LEFT_LEAN"
LABEL_USER_RIGHT = "USER_RIGHT_LEAN"
LABEL_HEAD_TILT = "HEAD_TILT_ONLY"
LABEL_TORSO_LEAN = "TORSO_LEAN_HEAD_UPRIGHT"
LABEL_USER_LEFT_MILD = "USER_LEFT_MILD"
LABEL_USER_LEFT_MEDIUM = "USER_LEFT_MEDIUM"
LABEL_USER_LEFT_LARGE = "USER_LEFT_LARGE"
LABEL_USER_RIGHT_MILD = "USER_RIGHT_MILD"
LABEL_USER_RIGHT_MEDIUM = "USER_RIGHT_MEDIUM"
LABEL_USER_RIGHT_LARGE = "USER_RIGHT_LARGE"
LABEL_LEFT_BOUNDARY_OUT = "LEFT_BOUNDARY_OUT"
LABEL_LEFT_BOUNDARY_RETURN = "LEFT_BOUNDARY_RETURN"
LABEL_RIGHT_BOUNDARY_OUT = "RIGHT_BOUNDARY_OUT"
LABEL_RIGHT_BOUNDARY_RETURN = "RIGHT_BOUNDARY_RETURN"

PROTOCOL_DEFAULT = "default"
PROTOCOL_LEAN_INTENSITY = "lean-intensity"
PROTOCOL_LEAN_THRESHOLD = "lean-threshold"

DEFAULT_PHASES = (
    LABEL_NEUTRAL,
    LABEL_USER_LEFT,
    LABEL_NEUTRAL,
    LABEL_USER_RIGHT,
    LABEL_NEUTRAL,
    LABEL_HEAD_TILT,
    LABEL_TORSO_LEAN,
)

LEAN_INTENSITY_PHASES = (
    LABEL_NEUTRAL,
    LABEL_USER_LEFT_MILD,
    LABEL_USER_LEFT_MEDIUM,
    LABEL_USER_LEFT_LARGE,
    LABEL_NEUTRAL,
    LABEL_USER_RIGHT_MILD,
    LABEL_USER_RIGHT_MEDIUM,
    LABEL_USER_RIGHT_LARGE,
    LABEL_NEUTRAL,
)

LEAN_THRESHOLD_PHASES = (
    LABEL_NEUTRAL,
    LABEL_LEFT_BOUNDARY_OUT,
    LABEL_LEFT_BOUNDARY_RETURN,
    LABEL_NEUTRAL,
    LABEL_RIGHT_BOUNDARY_OUT,
    LABEL_RIGHT_BOUNDARY_RETURN,
    LABEL_NEUTRAL,
)

BOUNDARY_MOVEMENT_LABELS = frozenset({
    LABEL_LEFT_BOUNDARY_OUT,
    LABEL_LEFT_BOUNDARY_RETURN,
    LABEL_RIGHT_BOUNDARY_OUT,
    LABEL_RIGHT_BOUNDARY_RETURN,
})

STATE_PREPARE = "PREPARE"
STATE_RECORDING = "RECORDING"
STATE_COMPLETE = "COMPLETE"
STATE_ABORTED = "ABORTED"

POSE_INSTRUCTIONS = {
    LABEL_NEUTRAL: (
        "Return to your calibrated/normal central sitting position.",
        "Face the webcam naturally.",
    ),
    LABEL_USER_LEFT: (
        "Move your upper body toward your own LEFT.",
        "Keep your face toward the webcam; do not intentionally tilt it.",
    ),
    LABEL_USER_RIGHT: (
        "Move your upper body toward your own RIGHT.",
        "Keep your face toward the webcam; do not intentionally tilt it.",
    ),
    LABEL_HEAD_TILT: (
        "Keep your torso approximately centered.",
        "Tilt your head sideways deliberately.",
    ),
    LABEL_TORSO_LEAN: (
        "Move your torso laterally.",
        "Try to keep your head approximately upright.",
    ),
    LABEL_USER_LEFT_MILD: (
        "Move your upper body slightly toward your own LEFT.",
        "Use a small, natural lean; face the webcam without a head tilt.",
    ),
    LABEL_USER_LEFT_MEDIUM: (
        "Move your upper body clearly toward your own LEFT.",
        "Keep it comfortable and sustainable; face the webcam without a head tilt.",
    ),
    LABEL_USER_LEFT_LARGE: (
        "Move your upper body markedly toward your own LEFT.",
        "Use a pronounced lean; face the webcam without a head tilt.",
    ),
    LABEL_USER_RIGHT_MILD: (
        "Move your upper body slightly toward your own RIGHT.",
        "Use a small, natural lean; face the webcam without a head tilt.",
    ),
    LABEL_USER_RIGHT_MEDIUM: (
        "Move your upper body clearly toward your own RIGHT.",
        "Keep it comfortable and sustainable; face the webcam without a head tilt.",
    ),
    LABEL_USER_RIGHT_LARGE: (
        "Move your upper body markedly toward your own RIGHT.",
        "Use a pronounced lean; face the webcam without a head tilt.",
    ),
    LABEL_LEFT_BOUNDARY_OUT: (
        "Start neutral, then slowly move toward your own LEFT while recording.",
        "Cross offset +0.20; face the webcam without an intentional head tilt.",
    ),
    LABEL_LEFT_BOUNDARY_RETURN: (
        "Start from the LEFT lean, then slowly return toward neutral.",
        "Cross offset +0.10 while recording.",
    ),
    LABEL_RIGHT_BOUNDARY_OUT: (
        "Start neutral, then slowly move toward your own RIGHT while recording.",
        "Cross offset -0.15; face the webcam without an intentional head tilt.",
    ),
    LABEL_RIGHT_BOUNDARY_RETURN: (
        "Start from the RIGHT lean, then slowly return toward neutral.",
        "Cross offset -0.08 while recording.",
    ),
}


@dataclass(frozen=True)
class LeanProtocol:
    """Ordered human prompts; only confirmed recording windows are timed."""

    record_sec: float = 3.0
    name: str = PROTOCOL_DEFAULT
    movement_sec: float = 5.0

    def __post_init__(self):
        if self.record_sec <= 0:
            raise ValueError("record_sec must be positive")
        if self.movement_sec <= 0:
            raise ValueError("movement_sec must be positive")
        if self.name not in (
            PROTOCOL_DEFAULT,
            PROTOCOL_LEAN_INTENSITY,
            PROTOCOL_LEAN_THRESHOLD,
        ):
            raise ValueError(f"unsupported protocol: {self.name}")

    @property
    def phases(self):
        if self.name == PROTOCOL_LEAN_INTENSITY:
            return LEAN_INTENSITY_PHASES
        if self.name == PROTOCOL_LEAN_THRESHOLD:
            return LEAN_THRESHOLD_PHASES
        return DEFAULT_PHASES

    def duration_for(self, label):
        if self.name == PROTOCOL_LEAN_THRESHOLD and label in BOUNDARY_MOVEMENT_LABELS:
            return self.movement_sec
        return self.record_sec


class GuidedLeanRecorder:
    """PREPARE -> explicit confirmation -> timed RECORDING state machine."""

    def __init__(self, protocol=None):
        self.protocol = protocol or LeanProtocol()
        self.stage_index = 0
        self.state = STATE_PREPARE
        self.recording_started_at = None

    @property
    def label(self):
        if self.state in (STATE_COMPLETE, STATE_ABORTED):
            return None
        return self.protocol.phases[self.stage_index]

    @property
    def is_recording(self):
        return self.state == STATE_RECORDING

    def confirm(self, now):
        """Start the current pose window only from PREPARE."""
        if self.state != STATE_PREPARE:
            return False
        self.state = STATE_RECORDING
        self.recording_started_at = float(now)
        return True

    def advance(self, now):
        """End a timed window; PREPARE never advances because time passed."""
        if self.state != STATE_RECORDING:
            return False
        duration = self.protocol.duration_for(self.label)
        if float(now) - self.recording_started_at < duration:
            return False
        self.stage_index += 1
        self.recording_started_at = None
        self.state = (
            STATE_COMPLETE
            if self.stage_index == len(self.protocol.phases)
            else STATE_PREPARE
        )
        return True

    def remaining_sec(self, now):
        if not self.is_recording:
            return None
        return max(
            self.protocol.duration_for(self.label)
            - (float(now) - self.recording_started_at),
            0.0,
        )

    def abort(self):
        if self.state == STATE_COMPLETE:
            return False
        self.state = STATE_ABORTED
        self.recording_started_at = None
        return True


def handle_key(workflow, key, now):
    """Apply preview controls and return ``confirm``, ``abort``, or ``None``."""
    if key in (27, ord("q"), ord("Q")):
        workflow.abort()
        return "abort"
    if key in (32, 10, 13):  # SPACE primary; ENTER accepted for convenience.
        return "confirm" if workflow.confirm(now) else None
    return None


def utc_timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def safe_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned or "unspecified"


def output_path(subject, condition, recorded_at=None):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(recorded_at))
    return OUT_DIR / f"{safe_name(subject)}_lean_{safe_name(condition)}_{stamp}.jsonl"


def frame_record(
    *,
    now,
    label,
    face_detected,
    frontal,
    calibration_available,
    geometry=None,
    offset=None,
    roll_delta=None,
):
    face_valid = bool(face_detected and frontal and geometry is not None)
    return {
        "type": "frame",
        "t": round(now, 4),
        "timestamp": utc_timestamp(now),
        "workflow_state": STATE_RECORDING,
        "guided_label": label,
        "face_detected": bool(face_detected),
        "frontal": None if frontal is None else bool(frontal),
        "face_valid": face_valid,
        "calibration_available": bool(calibration_available),
        "face_center_x_ratio": (
            None if geometry is None else round(geometry.center_x_ratio, 6)
        ),
        "face_width_ratio": (
            None if geometry is None else round(geometry.width_ratio, 6)
        ),
        "face_lateral_offset": None if offset is None else round(offset, 5),
        "head_roll_deg": (
            None if geometry is None else round(geometry.head_roll_deg, 4)
        ),
        "head_roll_delta_deg": (
            None if roll_delta is None else round(roll_delta, 4)
        ),
    }


def recording_frame_record(workflow, *, now, evaluator=None, **values):
    """Return a row only inside the confirmed window for the current pose."""
    workflow.advance(now)
    if not workflow.is_recording:
        return None
    row = frame_record(now=now, label=workflow.label, **values)
    if evaluator is not None:
        observation = evaluator.update(row["face_lateral_offset"])
        row.update({
            "candidate_lean_state": observation.direction,
            "candidate_left_entry": evaluator.policy.left_entry,
            "candidate_left_release": evaluator.policy.left_release,
            "candidate_right_entry": evaluator.policy.right_entry,
            "candidate_right_release": evaluator.policy.right_release,
            "candidate_transition": observation.transitioned,
        })
        if observation.transitioned:
            row["candidate_transition_from"] = observation.previous_direction
            row["candidate_transition_to"] = observation.direction
    return row


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--condition", default="unspecified")
    parser.add_argument("--cam", type=int, default=0)
    parser.add_argument(
        "--protocol",
        choices=(
            PROTOCOL_DEFAULT,
            PROTOCOL_LEAN_INTENSITY,
            PROTOCOL_LEAN_THRESHOLD,
        ),
        default=PROTOCOL_DEFAULT,
        help="guided pose sequence",
    )
    parser.add_argument(
        "--record-sec",
        type=float,
        default=3.0,
        help="SPACE 확인 뒤 각 안정 자세를 기록할 시간(초)",
    )
    parser.add_argument(
        "--movement-sec",
        type=float,
        default=5.0,
        help="lean-threshold 경계 이동 stage를 연속 기록할 시간(초)",
    )
    parser.add_argument("--calib-cm", type=float, default=60.0)
    parser.add_argument(
        "--recalibrate",
        action="store_true",
        help="recording 전에 개인 중립 calibration을 다시 수행",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        protocol = LeanProtocol(
            args.record_sec,
            name=args.protocol,
            movement_sec=args.movement_sec,
        )
    except ValueError as error:
        parser.error(str(error))

    calibration = Calibrator(calib_distance_cm=args.calib_cm)
    if args.recalibrate or calibration.lateral_baseline() is None:
        calibration.start()

    capture = cv2.VideoCapture(args.cam)
    if not capture.isOpened():
        sys.exit(f"카메라 {args.cam} 를 열 수 없습니다")
    try:
        detector = FaceLandmarks()
    except ImportError:
        capture.release()
        sys.exit("mediapipe 가 없습니다. pip install -r vision/requirements.txt")

    OUT_DIR.mkdir(exist_ok=True)
    recorded_at = time.time()
    path = output_path(args.subject, args.condition, recorded_at)
    output = path.open("w", encoding="utf-8")
    output.write(json.dumps({
        "type": "meta",
        "subject": args.subject,
        "condition": args.condition,
        "recorded_at": utc_timestamp(recorded_at),
        "formula": "(current_center_x_ratio-neutral_center_x_ratio)/current_width_ratio",
        "sign": "negative=image-left, positive=image-right; anatomical mapping unverified",
        "protocol": list(protocol.phases),
        "protocol_name": protocol.name,
        "workflow": "PREPARE_CONFIRM_RECORD",
        "record_sec": protocol.record_sec,
        "movement_sec": protocol.movement_sec,
    }, ensure_ascii=False) + "\n")

    print(f"[lean-rec] 저장 위치: {path}")
    print("[lean-rec] raw 영상 기준 음수=image-left, 양수=image-right")
    print("[lean-rec] 자세를 만든 뒤 SPACE를 누르면 3초 기록합니다")
    print("[lean-rec] ESC 또는 Q로 안전하게 종료합니다")
    workflow = GuidedLeanRecorder(protocol)
    candidate_evaluator = (
        FaceLeanClassifier()
        if protocol.name == PROTOCOL_LEAN_THRESHOLD else None
    )
    calibration_ready_announced = False
    frame_count = 0
    candidate_display = None

    try:
        while True:
            now = time.time()
            ok, frame = capture.read()
            if not ok:
                continue

            points = detector.detect(frame, now)
            detected = points is not None
            frontal = None
            width_px = None
            geometry = None
            ears = None
            if detected:
                frontal = is_frontal(points)
                width_px = face_width_px(points)
                ears = face_ear_values(points)
                if frontal:
                    geometry = face_lateral_geometry(points, frame.shape[1])

            if calibration.is_calibrating() and ears is not None:
                sample = calibration_sample(
                    face_detected=detected,
                    frontal=bool(frontal),
                    face_width_px=width_px,
                    left_ear=ears[0],
                    right_ear=ears[1],
                    combined_ear=ears[2],
                    face_center_x_ratio=(
                        None if geometry is None else geometry.center_x_ratio
                    ),
                    face_width_ratio=None if geometry is None else geometry.width_ratio,
                    head_roll_deg=None if geometry is None else geometry.head_roll_deg,
                )
                if sample is not None:
                    calibration.add_sample(sample, now=now)
            calibration.tick(now=now)

            baseline = calibration.lateral_baseline()
            if baseline is not None and not calibration_ready_announced:
                calibration_ready_announced = True
                print("[lean-rec] calibration 준비 완료 — 첫 자세를 준비하세요")

            label = "CALIBRATING"
            offset = roll_delta = None
            if baseline is not None:
                label = workflow.label
                if geometry is not None:
                    offset = lateral_offset(
                        geometry,
                        neutral_center_x_ratio=baseline["neutral_face_center_x_ratio"],
                    )
                    roll_delta = angle_delta_deg(
                        geometry.head_roll_deg,
                        baseline["neutral_head_roll_deg"],
                    )
                row = recording_frame_record(
                    workflow,
                    now=now,
                    evaluator=candidate_evaluator,
                    face_detected=detected,
                    frontal=frontal,
                    calibration_available=True,
                    geometry=geometry,
                    offset=offset,
                    roll_delta=roll_delta,
                )
                if row is not None:
                    output.write(json.dumps(row) + "\n")
                    frame_count += 1
                    if candidate_evaluator is not None:
                        candidate_display = row
                if workflow.state == STATE_COMPLETE:
                    print("[lean-rec] 모든 guided stage 기록 완료")
                    break

            if baseline is None:
                display_lines = ("[CALIBRATING]", "Face forward with eyes open.")
            elif workflow.state == STATE_PREPARE:
                display_lines = (
                    "[PREPARE]",
                    f"Target: {workflow.label}",
                    *POSE_INSTRUCTIONS[workflow.label],
                    "Press SPACE when the pose is stable.",
                )
            else:
                display_lines = (
                    "[RECORDING]",
                    workflow.label,
                    f"{workflow.remaining_sec(now):.1f} sec remaining",
                )
            if offset is not None:
                display_lines = (*display_lines, f"offset={offset:+.3f}")
            if candidate_display is not None and workflow.is_recording:
                candidate = candidate_display["candidate_lean_state"]
                if candidate_display["candidate_transition"]:
                    candidate = (
                        f"{candidate_display['candidate_transition_from']} -> "
                        f"{candidate_display['candidate_transition_to']}"
                    )
                display_lines = (
                    *display_lines,
                    f"candidate={candidate}",
                    f"roll_delta={roll_delta:+.1f} deg"
                    if roll_delta is not None else "roll_delta=NA",
                )
            for line_index, text in enumerate(display_lines):
                cv2.putText(
                    frame, text, (12, 30 + line_index * 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 255, 0), 2,
                )
            cv2.imshow("soma eval - lateral lean", frame)
            key = cv2.waitKey(1) & 0xFF
            if baseline is None and key not in (27, ord("q"), ord("Q")):
                action = None
            else:
                action = handle_key(workflow, key, now)
            if action == "abort":
                print("[lean-rec] 사용자 요청으로 기록을 중단했습니다")
                break
    except KeyboardInterrupt:
        pass
    finally:
        output.close()
        capture.release()
        detector.close()
        cv2.destroyAllWindows()
        print(f"[lean-rec] 저장: {path} ({frame_count} frames)")


if __name__ == "__main__":
    main()
