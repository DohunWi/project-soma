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
except ImportError:
    sys.exit("opencv 가 없습니다.  pip install -r vision/requirements.txt")

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from camera import default_index, list_cams, open_camera  # noqa: E402
from geometry import (face_width_cm_from_iris, face_width_px,  # noqa: E402
                      fov_deg_from_focal, focal_px_from_iris, is_frontal,
                      iris_px, yaw_asymmetry, IRIS_DIAMETER_CM)
from landmarks import FaceLandmarks  # noqa: E402

SAMPLE_SEC = 3.0


def measure(cap, det, label, seconds=SAMPLE_SEC):
    """N초 동안 정면 프레임의 얼굴 폭·홍채 지름 중앙값을 반환."""
    widths, irises, skipped, t0 = [], [], 0, time.time()
    while time.time() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            continue
        now = time.time()
        pts = det.detect(frame, now)
        if pts is not None:
            if is_frontal(pts):
                widths.append(face_width_px(pts))
                ip = iris_px(pts)
                if ip:
                    irises.append(ip)
            else:
                skipped += 1
        cv2.putText(frame, f"{label}  {time.time()-t0:.1f}/{seconds:.0f}s  n={len(widths)}",
                    (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow("soma eval — distance", frame)
        cv2.waitKey(1)
    if skipped:
        print(f"    (고개 돌림으로 {skipped}프레임 제외)")
    return (statistics.median(widths) if widths else None,
            statistics.median(irises) if irises else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=None, help=".env 의 WEBCAM_INDEX 를 씁니다")
    ap.add_argument("--list-cams", action="store_true", help="카메라 목록만 보고 종료")
    ap.add_argument("--calib-cm", type=float, default=60.0)
    ap.add_argument("--points", type=float, nargs="+",
                    default=[40, 50, 60, 70, 80])
    args = ap.parse_args()

    if args.list_cams:
        list_cams()
        return

    cam = default_index(args.cam)
    cap = open_camera(cam)
    print(f"[eval] 카메라 {cam}")
    try:
        det = FaceLandmarks()
    except ImportError:
        sys.exit("mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

    try:
        print("\n자로 재세요. 코끝에서 카메라 렌즈까지입니다.")
        print("노트북을 움직이지 마세요 — 각도가 바뀌면 상수가 흔들립니다.\n")

        rows = []
        for cm in args.points:
            input(f"[{len(rows)+1}/{len(args.points)}] {cm:.0f}cm 에 앉으신 뒤 Enter: ")
            px, ip = measure(cap, det, f"{cm:.0f}cm")
            if px is None:
                print("    검출 실패 — 건너뜁니다")
                continue
            rows.append({"cm": cm, "face_px": px, "iris_px": ip,
                         "f_face": px * cm, "f_iris": focal_px_from_iris(ip, cm) if ip else None,
                         "face_cm": face_width_cm_from_iris(px, ip) if ip else None})
            print(f"    얼굴폭 {px:.1f}px" + (f"   홍채 {ip:.1f}px" if ip else "   홍채 없음"))

        if not rows:
            sys.exit("측정된 지점이 없습니다")

        img_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        print(f"\n{'실제':>6} {'얼굴폭':>8} {'홍채':>7} {'f_px(홍채)':>11} {'얼굴폭cm':>9}")
        print("─" * 48)
        for r in rows:
            print(f"{r['cm']:6.0f} {r['face_px']:8.1f} "
                  f"{(r['iris_px'] or 0):7.1f} {(r['f_iris'] or 0):11.0f} "
                  f"{(r['face_cm'] or 0):9.1f}")

        # ── 초점거리: 지점마다 같은 값이 나와야 합니다 ────────────────────
        f_list = [r["f_iris"] for r in rows if r["f_iris"]]
        if f_list:
            f_med = statistics.median(f_list)
            spread = (max(f_list) - min(f_list)) / f_med * 100 if f_med else 0
            print(f"\nf_px(홍채) 중앙값 {f_med:.0f}   지점 간 편차 {spread:.1f}%"
                  f"   → 화각 {fov_deg_from_focal(img_w, f_med):.1f}°")
            print("편차가 크면 자를 잘못 쟀거나 고개 각도가 흔들린 것입니다.")

            # 이 값으로 각 지점을 되짚어 오차를 봅니다
            print(f"\n{'실제':>6} {'홍채추정':>9} {'오차':>7} {'상대오차':>9}")
            print("─" * 36)
            errs = []
            for r in rows:
                if not r["iris_px"]:
                    continue
                pred = f_med * IRIS_DIAMETER_CM / r["iris_px"]
                errs.append(abs(pred - r["cm"]))
                print(f"{r['cm']:6.0f} {pred:9.1f} {pred - r['cm']:+7.1f} "
                      f"{100*(pred-r['cm'])/r['cm']:+8.1f}%")
            if errs:
                mae = statistics.mean(errs)
                print(f"\n평균 절대오차 {mae:.1f}cm")
                print("판정: " + ("45cm 절대 임계를 쓸 수 있습니다" if mae <= 5
                                 else "5cm 를 넘습니다 — baseline 대비 변화량으로 바꾸세요"))

        face_cms = [r["face_cm"] for r in rows if r["face_cm"]]
        if face_cms:
            print(f"\n이 사람 얼굴폭 {statistics.median(face_cms):.1f}cm "
                  f"(홍채 {IRIS_DIAMETER_CM*10:.1f}mm 기준, 지점 간 편차 "
                  f"{(max(face_cms)-min(face_cms)):.2f}cm)")

        if f_list:
            print(f"\n.env 에 넣으세요:  VISION_FOCAL_PX={statistics.median(f_list):.0f}")
            print("이 값은 카메라 고유값이라 사람이 바뀌어도 유효합니다.")

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        cap.release()
        det.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
