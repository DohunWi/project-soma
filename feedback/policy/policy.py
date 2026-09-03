#!/usr/bin/env python3
"""
feedback/policy/policy.py
────────────────────────
state → feedback. 언제·무엇을·어느 강도로 말할지 정합니다.

    서버 'state'  →  이 프로세스  →  서버 'feedback'  →  LED / 진동 / 팝업

두 채널의 성격이 다릅니다.

  LED (앰비언트)  상시 미러링입니다. 개입이 아니라 상태의 반영이므로
                  게이트 없이 매번 갱신합니다. 주의 비용이 0입니다.
  진동 (명시)     개입입니다. 예산·침묵 규칙·신뢰도 게이트를 통과해야 발화합니다.

진동 패턴으로 의미를 구분하지 않습니다. 무엇이 문제인지는 LED 가 보여줍니다.
진동이 "봐라"고 부르고, 바가 "무엇을"을 답합니다.
패턴 학습을 사용자에게 요구하는 설계는 습관화 전에 버려집니다.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── 파라미터 ─────────────────────────────────────────────────────────
# 전부 추정치입니다. 실사용 로그로 스윕하기 전까지 확정값이 아닙니다.
CONF_MIN      = 0.5      # 이 미만이면 승격 금지. 강등은 허용 — 불확실하면 조용해집니다
SESSION_QUIET = 900.0    # 세션 시작 후 침묵 (초). 적응 시간
REPEAT_QUIET  = 1800.0   # 같은 이유로 다시 울리기까지 (초)
BUDGET_MAX    = 6        # 일일 진동 상한
SAT_FULL_SEC  = 0.0      # 채도 100% 기준
SAT_MIN_SEC   = 3000.0   # 이만큼 굳어 있으면 채도 20%


def to_led(metrics, state):
    """
    metrics → pos / width / sat.
    상시 미러링이므로 게이트가 없습니다.
    """
    if state == "ABSENT":
        return {"pos": 0.5, "width": 0.0, "sat": 0.0, "pulse": "none"}

    # 좌우: LEFT 면 왼쪽으로. 압력차의 크기는 아직 계약에 없어 3단계로만 씁니다
    pos = {"LEFT": 0.2, "RIGHT": 0.8}.get(metrics.get("balance"), 0.5)

    # 전후: 화면에 가까울수록 좁아집니다. 60cm 를 기준으로 40cm 에서 최소
    d = metrics.get("face_distance_cm")
    if d is None or d <= 0:
        width = 0.7
    else:
        width = 0.25 + 0.45 * min(max((d - 40.0) / 20.0, 0.0), 1.0)

    # 채도: 굳어 있을수록 빠지고, 움직이면 즉시 돌아옵니다.
    # 연속값(static_hold)을 씁니다. 누적값을 쓰면 움직여도 색이 안 돌아와
    # 피드백 루프가 닫히지 않습니다.
    hold = metrics.get("static_hold_sec") or 0.0
    k = min(max((hold - SAT_FULL_SEC) / (SAT_MIN_SEC - SAT_FULL_SEC), 0.0), 1.0)
    sat = 1.0 - 0.8 * k

    return {"pos": round(pos, 3), "width": round(width, 3),
            "sat": round(sat, 3), "pulse": "none"}


class VibrationPolicy:
    """진동 발화 판단. 순수 상태 + now 인자 — 테스트 가능합니다."""

    def __init__(self):
        self.budget = BUDGET_MAX
        self.day = None
        self.session_start = None
        self.last_fired = {}          # reason → t
        self.seated = False

    def decide(self, d, now):
        """발화할 진동 명령을 돌려주거나, 안 할 이유가 있으면 None."""
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.day:
            self.day, self.budget = day, BUDGET_MAX

        state = d.get("state")
        if state == "ABSENT":
            self.seated = False
            return None
        if not self.seated:                       # 착석 시작
            self.seated = True
            self.session_start = now

        if d.get("confidence", 0) < CONF_MIN:     # 불확실하면 조용히
            return None
        if state != "DANGER":                       # 진동은 DANGER 에서만
            return None
        if now - (self.session_start or now) < SESSION_QUIET:
            return None
        if self.budget <= 0:
            return None

        reasons = d.get("reasons") or []
        fresh = [r for r in reasons
                 if now - self.last_fired.get(r, -1e9) >= REPEAT_QUIET]
        if not fresh:
            return None

        for r in fresh:
            self.last_fired[r] = now
        self.budget -= 1

        return {"v": 1, "t": round(now, 3), "target": "chair_vibration",
                "action": {"pattern": "long1", "intensity": 180}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    args = ap.parse_args()

    try:
        import socketio
    except ImportError:
        sys.exit("python-socketio 가 없습니다.  pip install -r feedback/requirements.txt")

    sio = socketio.Client()
    vib = VibrationPolicy()

    @sio.on("state")
    def on_state(d):
        now = d.get("t") or time.time()

        sio.emit("feedback", {"v": 1, "t": round(now, 3), "target": "ambient_led",
                              "action": to_led(d.get("metrics", {}), d.get("state"))})

        cmd = vib.decide(d, now)
        if cmd:
            sio.emit("feedback", cmd)
            print(f"[policy] 진동 발화 — {d.get('reasons')} (예산 {vib.budget})",
                  file=sys.stderr)

    auth = os.getenv("SOCKET_AUTH_TOKEN")
    sio.connect(args.url, auth={"token": auth} if auth else None)
    print(f"[policy] 서버 연결: {args.url}", file=sys.stderr)
    try:
        sio.wait()
    except KeyboardInterrupt:
        pass
    finally:
        sio.disconnect()
        print("\n[policy] 종료", file=sys.stderr)


if __name__ == "__main__":
    main()
