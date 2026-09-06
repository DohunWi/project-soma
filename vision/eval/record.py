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
from ear import face_ear  # noqa: E402
from landmarks import FaceLandmarks  # noqa: E402

FACE_L, FACE_R = 234, 454
OUT_DIR = Path(__file__).parent / "data"

# 녹화 품질 하한. 이 아래면 스윕을 돌려도 의미 있는 임계가 나오지 않습니다.
MIN_DETECT_RATE = 0.80
MIN_CUES = 10

# 녹화 앞의 "눈 뜨고 버티기" 구간.
# 신호 방식만으로는 오검출을 셀 수 없습니다. 사람은 3~4초에 한 번 무의식적으로
# 깜빡이는데 신호 간격도 4초라, 신호 사이의 자연 깜빡임이 전부 오검출로 잡힙니다.
# 검출기가 맞게 잡은 것을 틀렸다고 세는 셈입니다.
# 이 구간에서는 피험자가 눈을 뜨고 버티므로, 여기서 잡힌 검출만이 진짜 오검출입니다.
HOLD_SEC = 15.0


def draw_overlay(frame, cue: bool, info: str, hold: bool = False):
    """
    미리보기 그리기. 프레임을 제자리에서 고칩니다.

    신호(cue)일 때는 **창 전체를 초록으로 덮습니다.** 화면 일부만 바꾸면
    피험자가 그것을 보려고 시선을 옮기게 되고, 시선이 움직이면 깜빡임
    타이밍이 흔들립니다. 전체를 덮으면 어디를 보고 있든 알아챕니다.

    루프 안에 있던 것을 함수로 뺐습니다. 프레임 크기를 여기서 구하지 않고
    바깥 변수(h, w)에 기대고 있었는데, 캡처 코드를 고치면서 그 정의가 사라져
    첫 신호에서 NameError 로 죽었습니다. 녹화가 5초에서 끝났고 아무도
    그 자리에서는 몰랐습니다.
    """
    h, w = frame.shape[:2]
    if cue:
        frame[:] = (0, 200, 0)                       # 신호: 전체 초록
        cv2.putText(frame, "BLINK", (w // 2 - 90, h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
    elif hold:
        # 버티기 구간은 파랗게. 초록(깜빡여라)과 색으로 구분합니다 —
        # 글씨를 읽어야 알 수 있으면 읽는 동안 이미 지나갑니다.
        frame[:] = (160, 90, 0)
        cv2.putText(frame, "EYES OPEN", (w // 2 - 150, h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 4)
        cv2.putText(frame, info, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 2)
    else:
        cv2.putText(frame, info, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 0), 2)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True, help="피험자 식별자 (예: S01)")
    ap.add_argument("--cam", type=int, default=None, help=".env 의 WEBCAM_INDEX 를 씁니다")
    ap.add_argument("--list-cams", action="store_true", help="카메라 목록만 보고 종료")
    ap.add_argument("--duration", type=float, default=90.0, help="초")
    ap.add_argument("--guided", action="store_true", help="신호에 맞춰 깜빡이기")
    ap.add_argument("--cue-interval", type=float, default=4.0, help="신호 간격(초)")
    ap.add_argument("--hold-sec", type=float, default=HOLD_SEC,
                    help="처음 N초는 눈을 뜨고 버팁니다. 진짜 오검출을 재는 구간입니다")
    ap.add_argument("--glasses", action="store_true", help="안경 착용 (메타데이터)")
    args = ap.parse_args()

    if args.list_cams:
        list_cams()
        return

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{args.subject}.jsonl"

    cam = default_index(args.cam)
    cap = open_camera(cam)
    print(f"[rec] 카메라 {cam}", file=sys.stderr)

    try:
        det = FaceLandmarks()
    except ImportError:
        sys.exit("mediapipe 가 없습니다.  pip install -r vision/requirements.txt")

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
        print(f"[rec] 1단계 — 처음 {args.hold_sec:.0f}초는 눈을 뜨고 버티세요 "
              f"(오검출 측정 구간)")
        print(f"[rec] 2단계 — 화면이 초록으로 바뀌면 한 번 깜빡이세요 "
              f"({args.cue_interval:.0f}초 간격)")
        print(f"[rec]         신호 사이에는 평소대로 깜빡이셔도 됩니다")
    else:
        print("[rec] 피험자가 깜빡일 때마다 관찰자가 스페이스바를 누르세요")
    print("[rec] ESC 로 종료")

    t0 = time.time()
    hold_end = t0 + (args.hold_sec if args.guided else 0.0)
    next_cue = hold_end + 2.0      # 버티기가 끝나고 2초 뒤부터 신호
    cue_until = 0.0
    n_cue = n_key = n_frame = n_detected = 0
    if args.guided and args.hold_sec > 0:
        f.write(json.dumps({"type": "hold", "start": round(t0, 4),
                            "end": round(hold_end, 4)}) + "\n")

    try:
        while True:
            now = time.time()
            if now - t0 > args.duration:
                break

            ok, frame = cap.read()
            if not ok:
                continue

            pts = det.detect(frame, now)

            ear = width_px = None
            if pts is not None:
                n_detected += 1
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
            holding = args.guided and now < hold_end
            if holding:
                info = f"눈을 뜨고 버티세요  {hold_end - now:4.1f}s"
            else:
                info = f"{now - t0:5.1f}s / {args.duration:.0f}s   " + \
                       (f"EAR {ear:.3f}" if ear else "no face") + \
                       (f"   cue {n_cue}" if args.guided else f"   key {n_key}")
            draw_overlay(frame, cue=args.guided and now < cue_until, info=info,
                         hold=holding)

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
        det.close()
        cv2.destroyAllWindows()
        gt = n_cue if args.guided else n_key
        detect_rate = n_detected / n_frame if n_frame else 0.0
        print(f"\n[rec] 저장: {out}")
        print(f"[rec] 프레임 {n_frame}개, 얼굴 검출 {n_detected}개 "
              f"({100 * detect_rate:.0f}%), 정답 {gt}개")

        # 못 쓰는 녹화를 조용히 저장하지 않습니다. 나중에 sweep 을 돌리고 나서야
        # "정답이 1개였다" 를 알면 그 사이 시간이 통째로 날아갑니다.
        problems = []
        if detect_rate < MIN_DETECT_RATE:
            problems.append(f"얼굴 검출률이 {100 * detect_rate:.0f}% 입니다 "
                            f"(권장 {100 * MIN_DETECT_RATE:.0f}% 이상). "
                            f"카메라·조명·앉은 위치를 확인하세요")
        if gt < MIN_CUES:
            problems.append(f"정답이 {gt}개뿐입니다 (권장 {MIN_CUES}개 이상). "
                            f"90초를 채워서 다시 찍으세요")
        if problems:
            print("\n[rec] ⚠ 이 녹화는 정확도 분석에 쓰기 어렵습니다")
            for p in problems:
                print(f"       - {p}")
            print(f"       다시:  python vision/eval/record.py --guided "
                  f"--subject {args.subject}")
        else:
            print(f"[rec] 다음:  python vision/eval/sweep.py {out}")


if __name__ == "__main__":
    main()
