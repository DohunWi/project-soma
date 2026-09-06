#!/usr/bin/env python3
"""
vision/run.py
─────────────
웹캠 → 거리 + 눈 깜빡임 → 서버.

    python vision/run.py                    # 서버로 전송
    python vision/run.py --stdout           # 서버 없이 jsonl
    python vision/run.py --preview          # 창에 EAR·거리 표시
    python vision/run.py --recalibrate      # 평소값 다시 잡기
    python vision/run.py --list-cams        # 쓸 수 있는 카메라 목록
    python vision/run.py --cam 1            # 카메라 고르기

**카메라 인덱스는 고정이 아닙니다.** macOS 의 연속성 카메라(Continuity Camera)가
근처의 iPhone 을 장치 목록에 끼워 넣고, 보통 그것이 0번을 가져갑니다.
iPhone 이 있냐 없냐에 따라 같은 인덱스가 다른 카메라를 가리킵니다.
--list-cams 로 확인하고 .env 의 WEBCAM_INDEX 를 박아 두세요.

**외부캠을 씁니다.** 내장캠은 각도에 예민해 값이 불안정합니다.
**영상은 서버로 보내지 않습니다.** 수치만 보냅니다.

보내는 형식은 docs/contracts/sensor_data.schema.json (source="vision") 입니다.
"""
import argparse
import json
import os
import sys
import time
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

from calibrator import Calibrator                       # vision/calibrator.py
from camera import default_index, list_cams, open_camera    # vision/camera.py
from distance import DistanceEstimator                  # vision/distance.py
from ear import BlinkCounter, face_ear                  # vision/blink/ear.py
from geometry import face_width_px, is_frontal, yaw_asymmetry   # vision/geometry.py
from landmarks import FaceLandmarks                     # vision/landmarks.py
from payload import vision_payload                      # vision/payload.py
from quality import FrameQuality                        # vision/quality.py

SEND_HZ = 2.0     # 서버 전송 주기. 깜빡임 사건은 발생 즉시 별도로 보냅니다


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=None, help=".env 의 WEBCAM_INDEX 를 씁니다")
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--user", default=os.getenv("USER_NAME", "guest"))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--list-cams", action="store_true",
                    help="쓸 수 있는 카메라를 훑어보고 종료")
    args = ap.parse_args()

    if args.list_cams:
        list_cams()
        return

    calib = Calibrator()   # 초점거리는 카메라를 연 뒤에 정해집니다
    if args.recalibrate:
        calib.start()

    emit = None
    if not args.stdout:
        try:
            import socketio
        except ImportError:
            sys.exit("python-socketio 가 없습니다.\n"
                     "  설치:        pip install -r vision/requirements.txt\n"
                     "  서버 없이 볼 때:  python vision/run.py --stdout --preview")
        sio = socketio.Client()
        auth = os.getenv("SOCKET_AUTH_TOKEN")
        sio.connect(args.url, auth={"token": auth} if auth else None)
        emit = lambda ev: sio.emit("sensor_data", ev)
        print(f"[vision] 서버 연결: {args.url}", file=sys.stderr)

    cam = default_index(args.cam)
    cap = open_camera(cam)
    print(f"[vision] 카메라 {cam} 시작", file=sys.stderr)

    # 카메라가 실제로 열린 뒤에 캘리브레이션을 시작합니다.
    # 열기 전에 시작하면 3초 창이 카메라 준비 대기에 소모돼 샘플이 1~2개만 모입니다.
    if calib.should_retry():
        calib.start()

    # mediapipe 를 직접 부르지 않습니다 — vision/landmarks.py 가 유일한 창구입니다
    try:
        det = FaceLandmarks()
    except ImportError:
        sys.exit("mediapipe 가 없습니다.  pip install -r vision/requirements.txt")
    print(f"[vision] 랜드마크 백엔드: {det.backend}", file=sys.stderr)

    # 거리 스케일은 카메라가 정합니다 — 사람이 60cm 에 앉는다고 가정하지 않습니다.
    est = DistanceEstimator(image_width_px=cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                            calibrator=calib)
    print(f"[vision] 거리: {est.describe()}", file=sys.stderr)

    # 평소 거리는 초점거리에 딸린 값입니다. 초점거리가 바뀌면 다시 잽니다.
    calib.bind_focal(est.focal_px)

    counter = BlinkCounter()
    quality = FrameQuality()
    last_send = 0.0

    def send(ev):
        if emit:
            emit(ev)
        else:
            print(json.dumps(ev, ensure_ascii=False), flush=True)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.03)
                continue

            now = time.time()
            calib.tick(now)        # 얼굴이 안 보여도 캘리브레이션 창은 끝나야 합니다
            pts = det.detect(frame, now)

            detected = pts is not None
            ear = dist_cm = width_px = yaw = None
            frontal = False
            blinked = False

            if detected:
                # 깜빡임은 좌우 회전에 견딥니다 — EAR 은 눈 안에서의 비율입니다
                ear = face_ear(pts)
                blinked = counter.update(ear, now)

                # 거리는 다릅니다. 고개를 돌리면 얼굴 폭이 투영상 줄어
                # "멀어졌다" 고 오판합니다. 정면일 때만 씁니다 (geometry.py 참조)
                # 거리는 홍채로 잽니다. 홍채는 시선을 향하므로 고개를 돌려도
                # 화면을 보고 있으면 원형에 가깝게 찍힙니다. 얼굴 폭은 고개가
                # 돌아가면 그대로 줄어 "멀어졌다" 고 오판했습니다.
                yaw = yaw_asymmetry(pts)
                frontal = is_frontal(pts)
                width_px = face_width_px(pts)
                dist_cm, dist_src = est.distance_cm(pts)
                if dist_src == "calib" and not frontal:
                    dist_cm = None          # 얼굴 폭 대비책은 정면일 때만 씁니다

                if calib.is_calibrating():
                    if dist_cm:
                        calib.add_sample({"distance_cm": dist_cm})
                elif calib.should_retry(now):
                    # 얼굴이 보이는데 평소 거리가 없으면 다시 시도합니다.
                    # 첫 시도는 보통 사람이 아직 자리에 앉기 전이라 실패합니다.
                    calib.start()

                # 평소 깜빡임은 세션 초반 5분에 걸쳐 잽니다. 얼굴이 보일 때만
                # 넣습니다 — 자리를 비우면 빈도가 0 으로 떨어지는데 그것이
                # 평소값에 섞이면 기준이 통째로 내려갑니다.
                calib.add_blink_sample(counter.rate(now), now)
                calib.add_distance_sample(dist_cm, now)

            quality.update(now, detected=detected,
                           frontal=(frontal if detected else None))
            rate = counter.rate(now)

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
                    calibrating=calib.is_calibrating())

            # 깜빡임 사건은 즉시 보냅니다 — 초 단위 사건이라 주기 전송에 묻히면 안 됩니다
            if blinked:
                send(build(True))

            if now - last_send >= 1.0 / SEND_HZ:
                last_send = now
                send(build(False))

            if args.preview:
                txt = (f"EAR {ear:.3f}  " if ear else "no face  ") + \
                      (f"{dist_cm:.0f}cm  " if dist_cm
                       else ("(고개 돌림)  " if detected and not frontal else "")) + \
                      (f"blink {rate:.1f}/min" if rate is not None
                       else f"blink 관측중 {counter.observed_sec(now):.0f}s")
                if calib.is_calibrating():
                    txt = f"CALIBRATING {calib.progress()*100:.0f}%  " + txt
                elif calib.needs_blink_baseline():
                    txt = f"평소깜빡임 {calib.blink_baseline_progress()*100:.0f}%  " + txt
                cv2.putText(frame, txt, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 255, 0), 2)
                cv2.imshow("soma vision", frame)
                if cv2.waitKey(1) & 0xFF == 27:      # ESC
                    break

    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        cap.release()
        det.close()
        if args.preview:
            cv2.destroyAllWindows()
        print("\n[vision] 종료", file=sys.stderr)


if __name__ == "__main__":
    main()
