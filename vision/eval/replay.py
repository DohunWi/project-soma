#!/usr/bin/env python3
"""
vision/eval/replay.py
─────────────────────
녹화한 EAR 로그를 **다시 돌려** 임계값이 실제 알림 수를 어떻게 바꾸는지 봅니다.
(architecture.md 가 말하는 *"임계 A 면 하루 알림 N 회"* 표의 입력)

    python vision/eval/replay.py vision/eval/data/S01.jsonl
    python vision/eval/replay.py "vision/eval/data/*.jsonl" --repeat 40
    python vision/eval/replay.py data/S01.jsonl --sweep
    python vision/eval/replay.py --self-test

sweep.py 는 *"깜빡임을 얼마나 정확히 세는가"* 를 봅니다.
이 도구는 그다음 질문을 봅니다 — *"그 숫자로 판정하면 사용자가 알림을
몇 번 받는가"*. 정확도가 좋아도 알림이 하루 40번이면 그 자리에서 꺼집니다.

**녹화는 짧고(90초) 판정 임계는 분 단위입니다.** 그래서 `--repeat` 로 같은
구간을 이어 붙여 긴 세션을 만듭니다. 사람이 같은 패턴을 반복한다는 가정이며,
근거 있는 추정이 아니라 **비교용**입니다. 임계 A 와 B 중 어느 쪽이 더 자주
울리는지를 같은 조건에서 보는 용도입니다.

의자 압력은 합성합니다. 매 샘플 30 씩 흔들어 "움직이는 중" 으로 둡니다.
그래야 정적 유지 타이머가 끼어들지 않고 **깜빡임 축만** 분리해서 볼 수 있습니다.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))
sys.path.insert(0, str(ROOT / "fusion"))

from ear import BlinkCounter, R_CLOSED, R_OPEN  # noqa: E402
import state as fusion                              # fusion/state.py  # noqa: E402

SEND_SEC = 0.5          # run.py 의 SEND_HZ = 2.0 과 같은 주기


def load_frames(paths):
    """record.py 가 남긴 jsonl → [(t, ear)] (시각은 0 부터)."""
    frames = []
    for path in paths:
        base = None
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("type") != "frame" or o.get("ear") is None:
                continue
            if base is None:
                base = o["t"]
            frames.append((o["t"] - base, o["ear"]))
    frames.sort()
    return frames


def synth_frames(seconds=90.0, blink_period=4.0, fps=30.0, fatigue=True):
    """
    합성 EAR 신호. 도구 자체 검증용.

    fatigue=True 면 깜빡임 간격이 4초에서 12초로 늘어납니다 (분당 15 → 5).
    임계를 바꿨을 때 알림 수가 실제로 달라지는지 보려면 값이 임계를
    가로질러야 합니다. 상수 신호로는 표가 전부 같은 줄이 됩니다.
    """
    out, t, phase = [], 0.0, 0.0
    while t < seconds:
        period = blink_period + (8.0 * t / seconds if fatigue else 0.0)
        closed = (t - phase) < 0.2
        if t - phase >= period:
            phase = t
        out.append((t, 0.10 if closed else 0.30))
        t += 1.0 / fps
    return out


def blink_series(frames, closed=R_CLOSED, open_th=R_OPEN, repeat=1):
    """
    EAR 시계열 → run.py 가 보냈을 (t, blink_rate) 목록.

    런타임과 같은 상태기계(BlinkCounter)를 씁니다. 임계는 평소 EAR 대비
    비율입니다 — 예전에는 모듈 상수를 임시로 바꿔 끼웠는데, 분석 도구가
    런타임 모듈의 전역을 건드리는 구조는 언제든 사고가 됩니다.
    """
    counter = BlinkCounter(r_closed=closed, r_open=open_th)
    span = (frames[-1][0] - frames[0][0]) if frames else 0.0
    out, next_send = [], 0.0
    for loop in range(repeat):
        offset = loop * (span + 1.0 / 30)
        for t, e in frames:
            now = offset + t
            counter.update(e, now)
            if now >= next_send:
                next_send = now + SEND_SEC
                out.append((now, counter.rate(now)))
    return out


def run_fusion(series, low=None, ok=None):
    """
    (t, blink_rate) → 상태 타임라인.

    압력은 매 샘플 흔들어 "움직이는 중" 으로 둡니다. 정적 유지 타이머가
    켜지면 깜빡임과 원인이 섞여 어느 축이 알림을 만들었는지 알 수 없습니다.
    """
    saved = (fusion.BLINK_RATE_LOW, fusion.BLINK_RATE_OK)
    if low is not None:
        fusion.BLINK_RATE_LOW = low
    if ok is not None:
        fusion.BLINK_RATE_OK = ok
    try:
        st = fusion.FusionState()
        prev, timeline = "NORMAL", []
        alerts = {"CAUTION": 0, "DANGER": 0}
        first_alert = None
        for i, (t, rate) in enumerate(series):
            # 네 채널을 같은 값으로 둡니다. 좌우 차이를 주면 balance 가 LEFT 로
            # 잡혀 imbalance 타이머가 300초에서 CAUTION 을 만듭니다 — 깜빡임과
            # 무관한 알림이 표에 섞입니다. 실제로 처음 짤 때 그렇게 나왔습니다.
            press = [850] * 4 if i % 2 == 0 else [880] * 4
            sample = {"pressure": press, "face_detected": True, "user_name": "replay"}
            if rate is not None:
                sample["blink_rate"] = rate
            st, d = fusion.step(st, sample, t)
            timeline.append((t, d["state"], rate, d["metrics"]["low_blink_sec"]))
            if d["state"] != prev and d["state"] in alerts:
                alerts[d["state"]] += 1
                if first_alert is None:
                    first_alert = t
            prev = d["state"]
        return {"timeline": timeline, "alerts": alerts, "first_alert_sec": first_alert,
                "low_blink_sec": timeline[-1][3] if timeline else 0.0,
                "duration_sec": timeline[-1][0] if timeline else 0.0}
    finally:
        fusion.BLINK_RATE_LOW, fusion.BLINK_RATE_OK = saved


def summarize(series):
    rates = [r for _, r in series if r is not None]
    if not rates:
        return {"n": 0}
    return {"n": len(rates), "min": min(rates), "max": max(rates),
            "avg": round(sum(rates) / len(rates), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--closed", type=float, default=R_CLOSED,
                    help="감김 임계 (평소 EAR 대비 비율)")
    ap.add_argument("--open", dest="open_th", type=float, default=R_OPEN,
                    help="뜸 임계 (평소 EAR 대비 비율)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="녹화를 N 번 이어 붙여 긴 세션을 만듭니다 (비교용 가정)")
    ap.add_argument("--sweep", action="store_true",
                    help="fusion 의 저깜빡임 임계를 훑어 알림 수를 비교합니다")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        frames = synth_frames()
        print("합성 신호 (90초, 4초마다 깜빡임 = 분당 15회)\n")
    else:
        files = [p for pat in args.paths for p in glob.glob(pat)]
        if not files:
            sys.exit("녹화 파일이 없습니다.  먼저: python vision/eval/record.py --guided --subject S01")
        frames = load_frames(files)
        print(f"프레임 {len(frames)}개  ({', '.join(Path(f).name for f in files)})\n")

    series = blink_series(frames, args.closed, args.open_th, args.repeat)
    stat = summarize(series)
    span = series[-1][0] if series else 0.0
    print(f"임계 CLOSED={args.closed} OPEN={args.open_th}  반복 {args.repeat}회  "
          f"→ {span/60:.1f}분")
    print(f"blink_rate  표본 {stat['n']}  평균 {stat.get('avg')}  "
          f"범위 {stat.get('min')}~{stat.get('max')}\n")

    if not args.sweep:
        res = run_fusion(series)
        print(f"저깜빡임 누적 {res['low_blink_sec']/60:.1f}분  "
              f"CAUTION {res['alerts']['CAUTION']}회  DANGER {res['alerts']['DANGER']}회")
        if res["first_alert_sec"] is not None:
            print(f"첫 알림까지 {res['first_alert_sec']/60:.1f}분")
        else:
            print("알림 없음")
        return

    print(f"{'LOW':>6} {'OK':>6}   {'CAUTION':>8} {'DANGER':>7} {'첫알림(분)':>10} "
          f"{'저깜빡임(분)':>12}")
    print("─" * 60)
    for low in (6.0, 7.0, 8.0, 9.0, 10.0):
        for ok in (low + 2.0, low + 3.0):
            res = run_fusion(series, low=low, ok=ok)
            first = res["first_alert_sec"]
            print(f"{low:6.1f} {ok:6.1f}   {res['alerts']['CAUTION']:8d} "
                  f"{res['alerts']['DANGER']:7d} "
                  f"{(first/60 if first is not None else float('nan')):10.1f} "
                  f"{res['low_blink_sec']/60:12.1f}")
    print("\n같은 로그·같은 검출로 임계만 바꾼 비교입니다. 절대 횟수가 아니라 "
          "상대 비교로 읽으세요.")


if __name__ == "__main__":
    main()
