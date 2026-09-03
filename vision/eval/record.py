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
    python vision/eval/record.py --guided --subject S01 --glasses
    python vision/eval/record.py --subject S02 --duration 120

기록 후:
    python vision/eval/sweep.py vision/eval/data/S01.jsonl
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "blink"))

try:
    import cv2
    import mediapipe as mp
except ImportError:
    sys.exit("opencv / mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

from ear import face_ear  # noqa: E402

FACE_L, FACE_R = 234, 454
OUT_DIR = Path(__file__).parent / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True, help="피험자 식별자 (예: S01)")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--duration", type=float, default=90.0, help="초")
    ap.add_argument("--guided", action="store_true", help="신호에 맞춰 깜빡이기")
    ap.add_argument("--cue-interval", type=float, default=4.0, help="신호 간격(초)")
    ap.add_argument("--glasses", action="store_true", help="안경 착용 (메타데이터)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{args.subject}.jsonl"

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        sys.exit(f"카메라 {args.cam} 를 열 수 없습니다")

    mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)

    f = out.open("w", encoding="utf-8")
    f.write(json.dumps({
        "type": "meta", "subject": args.subject, "glasses": args.glasses,
        "guided": args.guided, "cue_interval": args.cue_interval,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False) + "\n")

    print(f"[rec] {args.subject}  {args.duration:.0f}초  "
          f"{'신호 방식' if args.guided else '스페이스바 방식'}"
          f"{'  (안경)' if args.glasses else ''}")
    if args.guided:
        print(f"[rec] 화면이 초록으로 바뀌면 한 번 깜빡이세요 ({args.cue_interval:.0f}초 간격)")
    else:
        print("[rec] 피험자가 깜빡일 때마다 관찰자가 스페이스바를 누르세요")
    print("[rec] ESC 로 종료")

    t0 = time.time()
    next_cue = t0 + 5.0        # 처음 5초는 준비 시간
    cue_until = 0.0
    n_cue = n_key = n_frame = 0

    try:
        while True:
            now = time.time()
            if now - t0 > args.duration:
                break

            ok, frame = cap.read()
            if not ok:
                continue

            h, w = frame.shape[:2]
            res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            ear = width_px = None
            if res.multi_face_landmarks:
                lm = res.multi_face_landmarks[0].landmark
                pts = {i: (lm[i].x * w, lm[i].y * h) for i in range(len(lm))}
                ear = round(face_ear(pts), 5)
                lx, ly = pts[FACE_L]
                rx, ry = pts[FACE_R]
                width_px = round(((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5, 2)

            f.write(json.dumps({"type": "frame", "t": round(now, 4),
                                "ear": ear, "face_width_px": width_px}) + "\n")
            n_frame += 1

            # ── 정답 ──────────────────────────────────────────────
            if args.guided and now >= next_cue:
                f.write(json.dumps({"type": "cue", "t": round(now, 4)}) + "\n")
                n_cue += 1
                cue_until = now + 0.5
                next_cue = now + args.cue_interval

            # ── 화면 ──────────────────────────────────────────────
            if args.guided and now < cue_until:
                frame[:] = (0, 200, 0)                       # 신호: 전체 초록
                cv2.putText(frame, "BLINK", (w // 2 - 90, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
            else:
                info = f"{now - t0:5.1f}s / {args.duration:.0f}s   " + \
                       (f"EAR {ear:.3f}" if ear else "no face") + \
                       (f"   cue {n_cue}" if args.guided else f"   key {n_key}")
                cv2.putText(frame, info, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 255, 0), 2)

            cv2.imshow("soma eval — record", frame)
            k = cv2.waitKey(1) & 0xFF
            if k == 27:                                      # ESC
                break
            if k == 32 and not args.guided:                  # 스페이스바
                f.write(json.dumps({"type": "cue", "t": round(now, 4)}) + "\n")
                n_key += 1

    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        cap.release()
        mesh.close()
        cv2.destroyAllWindows()
        gt = n_cue if args.guided else n_key
        print(f"\n[rec] 저장: {out}")
        print(f"[rec] 프레임 {n_frame}개, 정답 {gt}개")
        print(f"[rec] 다음:  python vision/eval/sweep.py {out}")


if __name__ == "__main__":
    main()
