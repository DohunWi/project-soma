#!/usr/bin/env python3
"""
가짜 센서 스트림.

의자가 1대이고 공용공간에 있으므로, 하드웨어 없이 개발할 수 있어야 합니다.
docs/contracts/sensor_data.schema.json 을 그대로 따릅니다.

사용법:
    python tools/mock/stream.py                    # 서버로 전송 (기본)
    python tools/mock/stream.py --stdout           # 서버 없이 jsonl 출력
    python tools/mock/stream.py --scenario fatigue # 시나리오 선택
    python tools/mock/stream.py --speed 10         # 10배속 (긴 세션 빠르게)
"""
import argparse
import json
import math
import random
import sys
import time

SCENARIOS = ("normal", "fatigue", "imbalance", "absent")


def chair_sample(t, elapsed, scenario):
    """압력 4채널 [전좌, 전우, 후좌, 후우] + 적외선 1채널."""
    if scenario == "absent" and 60 < elapsed < 180:
        return {"pressure": [2, 1, 3, 2], "ir": [-1]}

    base = 850
    drift = min(elapsed / 1800, 1.0)          # 30분에 걸쳐 서서히 앞으로
    front = base + 150 * drift
    back = base - 100 * drift

    lean = 0.0
    if scenario == "imbalance":
        lean = 180 * min(elapsed / 900, 1.0)  # 15분에 걸쳐 한쪽으로

    noise = lambda: random.randint(-25, 25)
    return {
        "pressure": [
            int(front + lean + noise()), int(front - lean + noise()),
            int(back + lean + noise()),  int(back - lean + noise()),
        ],
        "ir": [int(250 + 400 * drift + random.randint(-20, 20))],
    }


def vision_sample(t, elapsed, scenario):
    """깜빡임 사건 + 분당 빈도 + 얼굴 거리."""
    if scenario == "absent" and 60 < elapsed < 180:
        return {"blink": False, "blink_rate": 0.0,
                "face_distance_cm": 0.0, "face_detected": False}

    rate = 15.0
    if scenario == "fatigue":
        rate = 15.0 - 9.0 * min(elapsed / 1200, 1.0)   # 20분에 걸쳐 15 → 6

    dist = 60.0 - 15.0 * min(elapsed / 1800, 1.0)      # 점점 가까워짐
    return {
        "blink": random.random() < rate / 60.0,        # 초당 확률
        "blink_rate": round(rate + random.uniform(-1, 1), 1),
        "face_distance_cm": round(dist + random.uniform(-2, 2), 1),
        "face_detected": True,
    }


def build(source, t, elapsed, scenario, user):
    ev = {"v": 1, "t": round(t, 3), "source": source, "user_name": user}
    if source == "chair":
        ev["device_id"] = "smart_chair_01"
        ev["chair"] = chair_sample(t, elapsed, scenario)
    else:
        ev["vision"] = vision_sample(t, elapsed, scenario)
    return ev


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:5000")
    p.add_argument("--stdout", action="store_true", help="서버 없이 jsonl 로 출력")
    p.add_argument("--scenario", choices=SCENARIOS, default="fatigue")
    p.add_argument("--speed", type=float, default=1.0, help="배속")
    p.add_argument("--hz", type=float, default=1.0, help="초당 샘플 수")
    p.add_argument("--user", default="mock_user")
    p.add_argument("--duration", type=float, default=0, help="초. 0 이면 무한")
    args = p.parse_args()

    emit = None
    if not args.stdout:
        try:
            import socketio
        except ImportError:
            sys.exit("python-socketio 가 없습니다.  pip install python-socketio[client]\n"
                     "또는 --stdout 으로 서버 없이 실행하세요.")
        sio = socketio.Client()
        sio.connect(args.url)
        emit = lambda ev: sio.emit("sensor_data", ev)
        print(f"연결됨: {args.url}  시나리오={args.scenario}  {args.speed}배속",
              file=sys.stderr)

    start = time.time()
    elapsed = 0.0
    step = 1.0 / args.hz
    try:
        while True:
            now = time.time()
            for source in ("chair", "vision"):
                ev = build(source, now, elapsed, args.scenario, args.user)
                if emit:
                    emit(ev)
                else:
                    print(json.dumps(ev, ensure_ascii=False), flush=True)
            elapsed += step
            if args.duration and elapsed >= args.duration:
                break
            time.sleep(step / args.speed)
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
