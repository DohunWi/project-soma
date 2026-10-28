#!/usr/bin/env python3
"""
vision/eval/record.py
─────────────────────
깜빡임 검출 **정확도 분석용 녹화**. (회의 항목 1의 후반)

EAR 시계열과 정답(ground truth)을 함께 jsonl 로 남깁니다.
검출 자체는 하지 않습니다 — 임계값을 바꿔가며 오프라인으로 재평가하려면
원본 EAR 이 남아 있어야 합니다.

정답을 만드는 두 가지 방법:

  --guided        화면의 신호에 맞춰 피험자가 깜빡입니다. 신호 시각이 정답입니다.
                  혼자 할 수 있고 정답이 정확합니다. **권장.**
  (기본)          관찰자가 옆에서 보다가 깜빡일 때 스페이스바를 누릅니다.
                  자연스러운 깜빡임을 잡지만 사람 반응 지연이 섞입니다.

사용법:
    python vision/eval/record.py --guided --subject S01 --condition light-off
    python vision/eval/record.py --guided --subject S01 --condition light-on
    python vision/eval/record.py --subject S02 --duration 120

기록 후:
    python vision/eval/sweep.py vision/eval/data/S01.jsonl
"""
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
    sys.exit("opencv 가 없습니다.  pip install -r vision/requirements.txt")

from ear import face_ear_values  # noqa: E402
from geometry import face_width_px, is_frontal  # noqa: E402
from landmarks import FaceLandmarks  # noqa: E402

OUT_DIR = Path(__file__).parent / "data"
LABEL_OPEN = "OPEN"
LABEL_BLINK = "BLINK"
LABEL_CLOSED_HOLD = "CLOSED_HOLD"
LABEL_UNLABELED = "UNLABELED"


@dataclass(frozen=True)
class GuidedProtocol:
    """Timed prompts used as labels; they are instructions, not detector output."""

    open_sec: float = 5.0
    blink_count: int = 10
    cue_interval_sec: float = 3.0
    blink_window_sec: float = 0.8
    closed_hold_sec: float = 2.0
    recovery_open_sec: float = 5.0

    def __post_init__(self):
        values = (
            self.open_sec,
            self.cue_interval_sec,
            self.blink_window_sec,
            self.closed_hold_sec,
            self.recovery_open_sec,
        )
        if self.blink_count < 1 or any(value <= 0 for value in values):
            raise ValueError("guided protocol 값은 모두 양수여야 합니다")
        if self.blink_window_sec >= self.cue_interval_sec:
            raise ValueError("blink window는 cue interval보다 짧아야 합니다")

    @property
    def cue_times(self):
        return tuple(
            self.open_sec + index * self.cue_interval_sec
            for index in range(self.blink_count)
        )

    @property
    def hold_start_sec(self):
        return self.open_sec + self.blink_count * self.cue_interval_sec

    @property
    def hold_end_sec(self):
        return self.hold_start_sec + self.closed_hold_sec

    @property
    def total_sec(self):
        return self.hold_end_sec + self.recovery_open_sec

    def label_at(self, elapsed_sec):
        if self.hold_start_sec <= elapsed_sec < self.hold_end_sec:
            return LABEL_CLOSED_HOLD
        if any(
            cue <= elapsed_sec < cue + self.blink_window_sec
            for cue in self.cue_times
        ):
            return LABEL_BLINK
        return LABEL_OPEN

    def due_cues(self, previous_sec, elapsed_sec):
        """Return every cue crossed since the previous frame."""
        return tuple(
            (index + 1, cue)
            for index, cue in enumerate(self.cue_times)
            if previous_sec < cue <= elapsed_sec
        )

    def metadata(self):
        return {
            "sequence": [LABEL_OPEN, LABEL_BLINK, LABEL_CLOSED_HOLD, LABEL_OPEN],
            "open_sec": self.open_sec,
            "blink_count": self.blink_count,
            "cue_interval_sec": self.cue_interval_sec,
            "blink_window_sec": self.blink_window_sec,
            "closed_hold_sec": self.closed_hold_sec,
            "recovery_open_sec": self.recovery_open_sec,
            "total_sec": self.total_sec,
        }


def utc_timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def safe_name(value):
    """Keep subject/condition metadata from becoming a filesystem path."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned or "unspecified"


def output_path(subject, condition, recorded_at=None):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(recorded_at))
    return OUT_DIR / f"{safe_name(subject)}_{safe_name(condition)}_{stamp}.jsonl"


def frame_record(
    *,
    now,
    label,
    left_ear=None,
    right_ear=None,
    combined_ear=None,
    detected=False,
    frontal=None,
    width_px=None,
):
    """Build one eval-only frame row while retaining sweep.py's ``ear`` alias."""
    return {
        "type": "frame",
        "t": round(now, 4),
        "timestamp": utc_timestamp(now),
        "left_ear": None if left_ear is None else round(left_ear, 5),
        "right_ear": None if right_ear is None else round(right_ear, 5),
        "combined_ear": None if combined_ear is None else round(combined_ear, 5),
        "ear": None if combined_ear is None else round(combined_ear, 5),
        "label": label,
        "face_detected": bool(detected),
        "frontal": None if frontal is None else bool(frontal),
        "face_width_px": None if width_px is None else round(width_px, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True, help="피험자 식별자 (예: S01)")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--duration", type=float, default=90.0, help="초")
    ap.add_argument("--guided", action="store_true", help="신호에 맞춰 깜빡이기")
    ap.add_argument("--cue-interval", type=float, default=4.0, help="신호 간격(초)")
    ap.add_argument("--blink-count", type=int, default=10, help="guided blink 횟수")
    ap.add_argument("--open-sec", type=float, default=5.0, help="시작/회복 OPEN 구간(초)")
    ap.add_argument("--blink-window-sec", type=float, default=0.8,
                    help="BLINK cue label 구간(초)")
    ap.add_argument("--closed-hold-sec", type=float, default=2.0,
                    help="눈을 계속 감는 CLOSED_HOLD 구간(초)")
    ap.add_argument("--condition", default="unspecified",
                    help="조명 등 recording 조건 (예: light-off, light-on)")
    ap.add_argument("--glasses", action="store_true", help="안경 착용 (메타데이터)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    recorded_at = time.time()
    out = output_path(args.subject, args.condition, recorded_at)
    try:
        protocol = GuidedProtocol(
            open_sec=args.open_sec,
            blink_count=args.blink_count,
            cue_interval_sec=args.cue_interval,
            blink_window_sec=args.blink_window_sec,
            closed_hold_sec=args.closed_hold_sec,
            recovery_open_sec=args.open_sec,
        ) if args.guided else None
    except ValueError as error:
        ap.error(str(error))
    run_duration = protocol.total_sec if protocol else args.duration

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        sys.exit(f"카메라 {args.cam} 를 열 수 없습니다")

    try:
        det = FaceLandmarks()
    except ImportError:
        sys.exit("mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

    f = out.open("w", encoding="utf-8")
    f.write(json.dumps({
        "type": "meta", "subject": args.subject, "glasses": args.glasses,
        "condition": args.condition,
        "guided": args.guided,
        "label_source": "guided_prompt_window" if args.guided else "unlabeled",
        "protocol": protocol.metadata() if protocol else None,
        "recorded_at": utc_timestamp(recorded_at),
    }, ensure_ascii=False) + "\n")

    print(f"[rec] {args.subject} / {args.condition}  {run_duration:.1f}초  "
          f"{'신호 방식' if args.guided else '스페이스바 방식'}"
          f"{'  (안경)' if args.glasses else ''}")
    if args.guided:
        print("[rec] 순서: OPEN → 자연스럽게 한 번 깜빡이기 반복 "
              "→ CLOSED_HOLD → OPEN")
        print(f"[rec] BLINK {protocol.blink_count}회, "
              f"CLOSED_HOLD {protocol.closed_hold_sec:.1f}초")
    else:
        print("[rec] 피험자가 깜빡일 때마다 관찰자가 스페이스바를 누르세요")
    print("[rec] ESC 로 종료")
    print(f"[rec] 저장 위치: {out}")

    t0 = time.time()
    previous_elapsed = -1e-9
    n_cue = n_key = n_frame = 0

    try:
        while True:
            now = time.time()
            elapsed = now - t0
            if elapsed > run_duration:
                break

            ok, frame = cap.read()
            if not ok:
                continue

            pts = det.detect(frame, now)
            h, w = frame.shape[:2]

            detected = pts is not None
            left_ear = right_ear = combined_ear = width_px = None
            frontal = None
            if detected:
                left_ear, right_ear, combined_ear = face_ear_values(pts)
                width_px = face_width_px(pts)
                frontal = is_frontal(pts)

            label = protocol.label_at(elapsed) if protocol else LABEL_UNLABELED
            f.write(json.dumps(frame_record(
                now=now,
                label=label,
                left_ear=left_ear,
                right_ear=right_ear,
                combined_ear=combined_ear,
                detected=detected,
                frontal=frontal,
                width_px=width_px,
            )) + "\n")
            n_frame += 1

            # ── 정답 ──────────────────────────────────────────────
            if protocol:
                for cue_index, scheduled_sec in protocol.due_cues(
                    previous_elapsed,
                    elapsed,
                ):
                    f.write(json.dumps({
                        "type": "cue",
                        "t": round(now, 4),
                        "timestamp": utc_timestamp(now),
                        "label": LABEL_BLINK,
                        "cue_index": cue_index,
                        "scheduled_elapsed_sec": scheduled_sec,
                    }) + "\n")
                    n_cue += 1
                previous_elapsed = elapsed

            # ── 화면 ──────────────────────────────────────────────
            if label == LABEL_BLINK:
                frame[:] = (0, 200, 0)                       # 신호: 전체 초록
                cv2.putText(frame, "BLINK ONCE", (w // 2 - 130, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
            elif label == LABEL_CLOSED_HOLD:
                frame[:] = (0, 80, 180)
                cv2.putText(frame, "KEEP EYES CLOSED", (w // 2 - 220, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
            else:
                info = f"{elapsed:5.1f}s / {run_duration:.1f}s   " + \
                       (f"EAR {combined_ear:.3f}" if combined_ear is not None
                        else "no face") + \
                       (f"   cue {n_cue}" if args.guided else f"   key {n_key}")
                cv2.putText(frame, info, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 255, 0), 2)
                if protocol:
                    cv2.putText(frame, "KEEP EYES OPEN / RELAX", (12, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            cv2.imshow("soma eval — record", frame)
            k = cv2.waitKey(1) & 0xFF
            if k == 27:                                      # ESC
                break
            if k == 32 and not args.guided:                  # 스페이스바
                f.write(json.dumps({
                    "type": "cue",
                    "t": round(now, 4),
                    "timestamp": utc_timestamp(now),
                    "label": LABEL_BLINK,
                }) + "\n")
                n_key += 1

    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        cap.release()
        det.close()
        cv2.destroyAllWindows()
        gt = n_cue if args.guided else n_key
        print(f"\n[rec] 저장: {out}")
        print(f"[rec] 프레임 {n_frame}개, 정답 {gt}개")
        print(f"[rec] 다음:  python vision/eval/sweep.py {out}")


if __name__ == "__main__":
    main()
