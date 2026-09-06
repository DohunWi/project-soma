#!/usr/bin/env python3
"""
vision/run.py
─────────────
웹캠 → 거리 + 눈 깜빡임 → 서버.

    python vision/run.py                    # 서버로 전송
    python vision/run.py --stdout           # 서버 없이 jsonl
    python vision/run.py --preview          # 창에 EAR·거리 표시
    python vision/run.py --recalibrate      # baseline 다시 잡기
    python vision/run.py --calib-cm 55      # 캘리브레이션 시 실제 거리(자로 잰 값)
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
import subprocess
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
from ear import BlinkCounter, face_ear                  # vision/blink/ear.py
from geometry import face_width_px, is_frontal, yaw_asymmetry   # vision/geometry.py
from landmarks import FaceLandmarks                     # vision/landmarks.py
from payload import vision_payload                      # vision/payload.py
from quality import FrameQuality                        # vision/quality.py

SEND_HZ = 2.0     # 서버 전송 주기. 깜빡임 사건은 발생 즉시 별도로 보냅니다
MAX_CAM_PROBE = 6 # --list-cams 가 훑어볼 인덱스 범위


def _mac_camera_names():
    """
    macOS 의 카메라 장치 이름 목록. 힌트로만 씁니다.

    OpenCV 는 장치 이름을 알려주지 않고 인덱스만 줍니다. 이름을 같이 보여주면
    "0번이 왜 iPhone 이지" 를 훨씬 빨리 알 수 있습니다. 다만 system_profiler 의
    순서가 AVFoundation 의 인덱스 순서와 같다는 보장은 없으므로, 매칭하지 않고
    목록만 보여줍니다.
    """
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and line.startswith("    ") and not line.startswith("      "):
            name = stripped[:-1]
            if name and name != "Camera":
                names.append(name)
    return names


def list_cams(max_index: int = MAX_CAM_PROBE) -> None:
    """
    열리는 카메라를 훑어 인덱스·해상도를 출력합니다.

    chair/bridge.py 의 --list 와 같은 이유로 있습니다. 장치 인덱스를
    코드나 머릿속에 박아 두면 환경이 바뀌었을 때 원인을 못 찾습니다.
    연속성 카메라는 iPhone 이 근처에 있을 때만 목록에 나타나므로,
    같은 인덱스가 어제와 다른 카메라를 가리킬 수 있습니다.
    """
    try:
        cv2.setLogLevel(0)            # 없는 인덱스를 열 때 나오는 경고를 줄입니다
    except AttributeError:
        pass

    for name in _mac_camera_names():
        print(f"  (이름) {name}")
    if sys.platform == "darwin":
        print("  ※ 이름 순서와 인덱스 순서는 다릅니다. 연속성 카메라(iPhone)가")
        print("     0번을 가져가는 경우가 많습니다.\n")

    found = 0
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if not cap.isOpened():
            cap.release()
            continue
        ok, frame = cap.read()
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if not ok or frame is None:
            print(f"  [{i}] 열렸지만 프레임이 오지 않습니다 (다른 앱이 쓰는 중일 수 있습니다)")
            continue
        found += 1
        print(f"  [{i}] {w}x{h}")
    if not found:
        print("  쓸 수 있는 카메라가 없습니다.")
        print("  macOS 라면 시스템 설정 → 개인정보 보호 및 보안 → 카메라에서")
        print("  터미널 앱을 허용하고 터미널을 다시 시작하세요.")
    else:
        print("\n어느 것이 내장캠인지 --preview 로 확인한 뒤")
        print(".env 의 WEBCAM_INDEX 에 박아 두세요. iPhone 이 근처에 있으면")
        print("연속성 카메라가 목록에 끼어들어 인덱스가 밀립니다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=int(os.getenv("WEBCAM_INDEX", 0)))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--user", default=os.getenv("USER_NAME", "guest"))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--calib-cm", type=float, default=60.0)
    ap.add_argument("--list-cams", action="store_true",
                    help="쓸 수 있는 카메라를 훑어보고 종료")
    args = ap.parse_args()

    if args.list_cams:
        list_cams()
        return

    calib = Calibrator(calib_distance_cm=args.calib_cm)
    if args.recalibrate:
        calib.start()

    emit = None
    if not args.stdout:
        try:
            import socketio
        except ImportError:
            sys.exit("python-socketio 가 없습니다.  pip install -r vision/requirements.txt")
        sio = socketio.Client()
        auth = os.getenv("SOCKET_AUTH_TOKEN")
        sio.connect(args.url, auth={"token": auth} if auth else None)
        emit = lambda ev: sio.emit("sensor_data", ev)
        print(f"[vision] 서버 연결: {args.url}", file=sys.stderr)

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        sys.exit(f"카메라 {args.cam} 를 열 수 없습니다. --cam 으로 다른 인덱스를 시도하세요.")
    print(f"[vision] 카메라 {args.cam} 시작", file=sys.stderr)

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
                yaw = yaw_asymmetry(pts)
                frontal = is_frontal(pts)
                width_px = face_width_px(pts)
                if frontal:
                    dist_cm = calib.distance_cm(width_px)

                if calib.is_calibrating() and frontal:
                    calib.add_sample({"face_width_px": width_px,
                                      "blink_rate": counter.rate(now)})

                # 얼굴이 보이는데 baseline 이 없으면 다시 시도합니다.
                # 첫 시도는 보통 사람이 아직 자리에 앉기 전이라 실패합니다.
                elif calib.should_retry(now):
                    calib.start()

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
