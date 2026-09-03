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
    import mediapipe as mp
except ImportError:
    sys.exit("opencv / mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

from calibrator import Calibrator          # vision/calibrator.py
from ear import BlinkCounter, face_ear     # vision/blink/ear.py

# 얼굴 폭: Face Mesh 좌우 얼굴 경계. 좌우 회전에 비교적 안정적입니다
FACE_L, FACE_R = 234, 454

SEND_HZ = 2.0     # 서버 전송 주기. 깜빡임 사건은 발생 즉시 별도로 보냅니다


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=int(os.getenv("WEBCAM_INDEX", 0)))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--user", default=os.getenv("USER_NAME", "guest"))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--calib-cm", type=float, default=60.0)
    args = ap.parse_args()

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
    if not calib.is_done() and not calib.is_calibrating():
        calib.start()

    mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)

    counter = BlinkCounter()
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
            h, w = frame.shape[:2]
            res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            detected = bool(res.multi_face_landmarks)
            ear = dist_cm = width_px = None
            blinked = False

            if detected:
                lm = res.multi_face_landmarks[0].landmark
                pts = {i: (lm[i].x * w, lm[i].y * h) for i in range(len(lm))}
                ear = face_ear(pts)
                blinked = counter.update(ear, now)

                lx, ly = pts[FACE_L]
                rx, ry = pts[FACE_R]
                width_px = ((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5
                dist_cm = calib.distance_cm(width_px)

                if calib.is_calibrating():
                    calib.add_sample({"face_width_px": width_px,
                                      "blink_rate": counter.rate(now)})

            rate = counter.rate(now)

            # 깜빡임 사건은 즉시 보냅니다 — 초 단위 사건이라 주기 전송에 묻히면 안 됩니다
            if blinked:
                send({"v": 1, "t": round(now, 3), "source": "vision",
                      "user_name": args.user,
                      "vision": {"blink": True, "blink_rate": rate,
                                 "face_distance_cm": dist_cm,
                                 "face_detected": True}})

            if now - last_send >= 1.0 / SEND_HZ:
                last_send = now
                send({"v": 1, "t": round(now, 3), "source": "vision",
                      "user_name": args.user,
                      "vision": {"blink": False, "blink_rate": rate,
                                 "face_distance_cm": dist_cm,
                                 "face_detected": detected}})

            if args.preview:
                txt = (f"EAR {ear:.3f}  " if ear else "no face  ") + \
                      (f"{dist_cm:.0f}cm  " if dist_cm else "") + \
                      f"blink {rate:.1f}/min"
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
        mesh.close()
        if args.preview:
            cv2.destroyAllWindows()
        print("\n[vision] 종료", file=sys.stderr)


if __name__ == "__main__":
    main()
