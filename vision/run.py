#!/usr/bin/env python3
"""
vision/run.py
─────────────
웹캠 → 거리 + 눈 깜빡임 → 서버.

    python vision/run.py                    # 서버로 전송
    python vision/run.py --stdout           # 서버 없이 jsonl
    python vision/run.py --preview          # 창에 EAR·거리 표시
    python vision/run.py --stdout --debug-blink  # blink 진단은 stderr
    python vision/run.py --recalibrate      # baseline 다시 잡기
    python vision/run.py --calib-cm 55      # 캘리브레이션 시 실제 거리(자로 잰 값)

**외부캠을 씁니다.** 내장캠은 각도에 예민해 값이 불안정합니다.
**영상은 서버로 보내지 않습니다.** 수치만 보냅니다.

보내는 형식은 docs/contracts/sensor_data.schema.json (source="vision") 입니다.
"""
import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    import cv2
except ImportError:
    sys.exit("opencv 가 없습니다.  pip install -r vision/requirements.txt")

from calibrator import Calibrator, calibration_sample   # vision/calibrator.py
from config import resolve_ear_thresholds               # vision/config.py
from ear import (  # vision/blink/ear.py
    BlinkCounter,
    emit_blink_diagnostic,
    face_ear_values,
)
from geometry import face_width_px, is_frontal, yaw_asymmetry   # vision/geometry.py
from lateral import (
    FaceLeanClassifier,
    angle_delta_deg,
    face_lateral_geometry,
    lateral_offset,
)
from landmarks import FaceLandmarks                     # vision/landmarks.py
from payload import vision_payload                      # vision/payload.py
from quality import FrameQuality                        # vision/quality.py
from runtime import (CameraManager, FrameResult, StdoutTransport,
                     VisionSocketTransport, run_guarded, run_vision_loop)

SEND_HZ = 2.0     # 서버 전송 주기. 깜빡임 사건은 발생 즉시 별도로 보냅니다
LOG = logging.getLogger("soma.vision")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=int(os.getenv("WEBCAM_INDEX", 0)))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--user", default=os.getenv("USER_NAME", "guest"))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument(
        "--debug-blink",
        action="store_true",
        help="좌우/평균 EAR와 blink 상태 전이를 stderr에 출력",
    )
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--calib-cm", type=float, default=60.0)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info(
        "Vision starting camera=%d backend=%s mode=%s",
        args.cam,
        args.url,
        "stdout" if args.stdout else "socket",
    )

    resources = {"camera": None, "detector": None, "transport": None}

    def initialize_and_run():
        calib = Calibrator(calib_distance_cm=args.calib_cm)
        if args.recalibrate:
            calib.start()

        if args.stdout:
            transport = StdoutTransport(
                lambda event: print(
                    json.dumps(event, ensure_ascii=False), flush=True
                )
            )
        else:
            try:
                import socketio
            except ImportError as exc:
                raise RuntimeError(
                    "python-socketio 가 없습니다. "
                    "pip install -r vision/requirements.txt"
                ) from exc
            transport = VisionSocketTransport(
                socketio.Client(reconnection=False),
                args.url,
                os.getenv("SOCKET_AUTH_TOKEN"),
            )
        resources["transport"] = transport

        # mediapipe 를 직접 부르지 않습니다 — vision/landmarks.py 가 유일한 창구입니다
        try:
            det = FaceLandmarks()
        except ImportError as exc:
            raise RuntimeError(
                "mediapipe 가 없습니다. pip install -r vision/requirements.txt"
            ) from exc
        resources["detector"] = det
        LOG.info("Vision detector backend=%s", det.backend)
        LOG.info(
            "Vision calibration status=%s",
            "recalibrating" if args.recalibrate else (
                "ready" if calib.is_done() else "pending"
            ),
        )

        camera = CameraManager(cv2.VideoCapture, args.cam)
        resources["camera"] = camera
        transport.start()

        def make_blink_counter():
            selection = resolve_ear_thresholds(calib.open_ear_baseline())
            if args.debug_blink:
                baseline = (
                    f"{selection.open_ear_baseline:.4f}"
                    if selection.open_ear_baseline is not None
                    else "NA"
                )
                print(
                    f"[blink-debug] threshold_mode="
                    f"{'personalized' if selection.personalized else 'fallback'} "
                    f"open_ear_baseline={baseline} "
                    f"closed_threshold={selection.closed_threshold:.4f} "
                    f"open_threshold={selection.open_threshold:.4f}",
                    file=sys.stderr,
                    flush=True,
                )
            return BlinkCounter(
                closed_threshold=selection.closed_threshold,
                open_threshold=selection.open_threshold,
            )

        counter = make_blink_counter()
        lean_classifier = FaceLeanClassifier()
        quality = FrameQuality()
        last_send = 0.0

        def process_frame(frame, now):
            nonlocal counter, last_send

            # 카메라가 실제로 열린 뒤에 캘리브레이션을 시작합니다.
            # 열기 전에 시작하면 3초 창이 준비 대기에 소모됩니다.
            if not calib.is_done() and not calib.is_calibrating():
                calib.start()
            was_calibrating = calib.is_calibrating()

            pts = det.detect(frame, now)

            detected = pts is not None
            left_ear = right_ear = ear = dist_cm = width_px = yaw = None
            lateral_geometry = None
            face_lateral_offset = head_roll_deg = head_roll_delta_deg = None
            frontal = False
            blinked = False
            blink_phase_before = counter.phase

            if detected:
                # 깜빡임은 좌우 회전에 견딥니다 — EAR 은 눈 안에서의 비율입니다
                left_ear, right_ear, ear = face_ear_values(pts)
                ear_is_finite = all(
                    math.isfinite(value) for value in (left_ear, right_ear, ear)
                )
                if ear_is_finite:
                    blinked = counter.update(ear, now)
                else:
                    counter.on_face_lost()

                # 거리는 다릅니다. 고개를 돌리면 얼굴 폭이 투영상 줄어
                # "멀어졌다" 고 오판합니다. 정면일 때만 씁니다 (geometry.py 참조)
                yaw = yaw_asymmetry(pts)
                frontal = is_frontal(pts)
                width_px = face_width_px(pts)
                if frontal:
                    dist_cm = calib.distance_cm(width_px)
                    lateral_geometry = face_lateral_geometry(pts, frame.shape[1])

                if calib.is_calibrating():
                    sample = calibration_sample(
                        face_detected=detected,
                        frontal=frontal,
                        face_width_px=width_px,
                        left_ear=left_ear,
                        right_ear=right_ear,
                        combined_ear=ear,
                        blink_rate=counter.rate(now),
                        face_center_x_ratio=(
                            lateral_geometry.center_x_ratio
                            if lateral_geometry is not None else None
                        ),
                        face_width_ratio=(
                            lateral_geometry.width_ratio
                            if lateral_geometry is not None else None
                        ),
                        head_roll_deg=(
                            lateral_geometry.head_roll_deg
                            if lateral_geometry is not None else None
                        ),
                    )
                    if sample is not None:
                        calib.add_sample(sample, now=now)
            else:
                # Face loss invalidates any partial closure. A later face must
                # start a new candidate instead of completing the old one.
                counter.on_face_lost()

            calib.tick(now=now)
            if was_calibrating and not calib.is_calibrating():
                # Startup samples must not leak into the measured blink window.
                # A failed calibration resolves to the prior baseline or fallback.
                counter = make_blink_counter()
                lean_classifier.reset()
                blinked = False
                blink_phase_before = counter.phase

            lateral_baseline = calib.lateral_baseline()
            if lateral_geometry is not None:
                head_roll_deg = round(lateral_geometry.head_roll_deg, 3)
                if lateral_baseline is not None:
                    face_lateral_offset = lateral_offset(
                        lateral_geometry,
                        neutral_center_x_ratio=lateral_baseline[
                            "neutral_face_center_x_ratio"
                        ],
                    )
                    head_roll_delta_deg = angle_delta_deg(
                        lateral_geometry.head_roll_deg,
                        lateral_baseline["neutral_head_roll_deg"],
                    )
                    if face_lateral_offset is not None:
                        face_lateral_offset = round(face_lateral_offset, 4)
                    if head_roll_delta_deg is not None:
                        head_roll_delta_deg = round(head_roll_delta_deg, 3)
            face_lean_direction = lean_classifier.update(
                face_lateral_offset
            ).direction

            quality.update(now, detected=detected,
                           frontal=(frontal if detected else None))
            rate = counter.rate(now)
            emit_blink_diagnostic(
                args.debug_blink,
                now=now,
                left_ear=left_ear,
                right_ear=right_ear,
                combined_ear=ear,
                phase_before=blink_phase_before,
                counter=counter,
                blinked=blinked,
            )

            # payload 조립은 vision/payload.py 의 순수 함수가 합니다.
            # 값이 없으면 키를 생략합니다 — null 을 보내면 스키마 위반이라
            # 서버가 payload 를 통째로 버리고, 거리 하나 때문에 깜빡임까지
            # 같이 사라집니다 (docs/contracts/README.md 규칙 5).
            def build(blink):
                return vision_payload(
                    t=now, user_name=args.user, face_detected=detected, blink=blink,
                    blink_rate=rate,
                    blink_count=counter.count(now),
                    window_sec=counter.observed_sec(now),
                    blink_duration_ms=counter.last_duration_ms if blink else None,
                    face_distance_cm=dist_cm,
                    face_distance_baseline_cm=calib.baseline_distance_cm(),
                    blink_rate_baseline=calib.baseline_blink_rate(),
                    detect_rate=quality.detect_rate(now),
                    yaw_dropped_rate=quality.yaw_dropped_rate(now),
                    calibrating=calib.is_calibrating(),
                    face_lateral_offset=face_lateral_offset,
                    head_roll_deg=head_roll_deg,
                    head_roll_delta_deg=head_roll_delta_deg,
                    face_lateral_calibrated=lateral_baseline is not None,
                    face_lean_direction=face_lean_direction)

            payloads = []
            # 깜빡임 사건은 즉시 보냅니다 — 초 단위 사건이라 주기 전송에 묻히면 안 됩니다
            if blinked:
                payloads.append(build(True))

            if now - last_send >= 1.0 / SEND_HZ:
                last_send = now
                payloads.append(build(False))

            stop = False
            if args.preview:
                txt = (f"EAR {ear:.3f}  " if ear else "no face  ") + \
                      (f"{dist_cm:.0f}cm  " if dist_cm
                       else ("(고개 돌림)  " if detected and not frontal else "")) + \
                      (f"blink {rate:.1f}/min" if rate is not None
                       else f"blink 관측중 {counter.observed_sec(now):.0f}s")
                if calib.is_calibrating():
                    txt = f"CALIBRATING {calib.progress()*100:.0f}%  " + txt
                cv2.putText(frame, txt, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 255, 0), 2)
                cv2.imshow("soma vision", frame)
                if cv2.waitKey(1) & 0xFF == 27:      # ESC
                    stop = True
            return FrameResult(payloads=payloads, stop=stop)

        run_vision_loop(camera, process_frame, transport)

    def close_resource(name):
        resource = resources[name]
        if resource is not None:
            resource.close()

    cleanups = [
        ("camera", lambda: close_resource("camera")),
        ("detector", lambda: close_resource("detector")),
        ("socket", lambda: close_resource("transport")),
    ]
    if args.preview:
        cleanups.append(("preview windows", cv2.destroyAllWindows))

    return run_guarded(initialize_and_run, cleanups)


if __name__ == "__main__":
    raise SystemExit(main())
