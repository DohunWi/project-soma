#!/usr/bin/env python3
"""
vision/eval/distance_check.py
─────────────────────────────
**거리 추정 정확도**를 잽니다.

핀홀 근사(face_width_px × distance = 상수)가 실제로 얼마나 맞는지
자로 잰 거리와 비교합니다. 캘리브레이션에 쓴 거리 하나로 나머지 거리들이
얼마나 잘 나오는지가 핵심입니다.

    python vision/eval/distance_check.py --calib-cm 60 --points 40 50 60 70 80

진행:
  1. 캘리브레이션 거리(기본 60cm)에 앉아 baseline 을 잡습니다
  2. 각 거리로 옮겨 앉으며 Enter 를 누릅니다
  3. 예측값과 실측값의 오차 표가 나옵니다

**자로 재세요.** 캘리브레이션 거리가 틀리면 상수가 통째로 틀립니다.
얼굴(코 끝) 에서 카메라 렌즈까지를 잽니다.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))

try:
    import cv2
    import mediapipe as mp
except ImportError:
    sys.exit("opencv / mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

from geometry import face_width_px, is_frontal, yaw_asymmetry  # noqa: E402

SAMPLE_SEC = 3.0


def measure(cap, mesh, label, seconds=SAMPLE_SEC):
    """N초 동안 정면 프레임의 얼굴 폭 중앙값을 반환."""
    widths, skipped, t0 = [], 0, time.time()
    while time.time() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if res.multi_face_landmarks:
            lm = res.multi_face_landmarks[0].landmark
            pts = {i: (lm[i].x * w, lm[i].y * h) for i in range(len(lm))}
            if is_frontal(pts):
                widths.append(face_width_px(pts))
            else:
                skipped += 1
        cv2.putText(frame, f"{label}  {time.time()-t0:.1f}/{seconds:.0f}s  n={len(widths)}",
                    (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow("soma eval — distance", frame)
        cv2.waitKey(1)
    if skipped:
        print(f"    (고개 돌림으로 {skipped}프레임 제외)")
    return statistics.median(widths) if widths else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--calib-cm", type=float, default=60.0)
    ap.add_argument("--points", type=float, nargs="+",
                    default=[40, 50, 60, 70, 80])
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        sys.exit(f"카메라 {args.cam} 를 열 수 없습니다")
    mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)

    try:
        input(f"\n[1] 카메라에서 정확히 {args.calib_cm:.0f}cm 에 앉으신 뒤 Enter: ")
        base_px = measure(cap, mesh, f"CALIB {args.calib_cm:.0f}cm")
        if base_px is None:
            sys.exit("얼굴을 검출하지 못했습니다")
        k = base_px * args.calib_cm
        print(f"    baseline = {base_px:.1f}px  →  상수 k = {k:.0f}\n")

        rows = []
        for cm in args.points:
            input(f"[2] {cm:.0f}cm 로 옮겨 앉으신 뒤 Enter: ")
            px = measure(cap, mesh, f"{cm:.0f}cm")
            if px is None:
                print("    검출 실패 — 건너뜁니다")
                continue
            pred = k / px
            rows.append((cm, px, pred, pred - cm))

        print(f"\n{'실제':>6} {'얼굴폭':>8} {'예측':>7} {'오차':>7} {'상대오차':>8}")
        print("─" * 42)
        for cm, px, pred, err in rows:
            print(f"{cm:6.0f} {px:8.1f} {pred:7.1f} {err:+7.1f} {100*err/cm:+7.1f}%")

        if rows:
            errs = [abs(e) for *_, e in rows]
            rel = [abs(e) / cm * 100 for cm, _, _, e in rows]
            print(f"\n평균 절대오차 {statistics.mean(errs):.1f}cm  "
                  f"(상대 {statistics.mean(rel):.1f}%)   최대 {max(errs):.1f}cm")
            print("\n판정 기준: fusion 의 근접 임계는 45cm 입니다.")
            print("평균 오차가 5cm 를 넘으면 그 임계로 판정하기 어렵습니다 —")
            print("절대 거리 대신 baseline 대비 변화량을 쓰는 쪽으로 바꾸세요.")
    finally:
        cap.release()
        mesh.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
