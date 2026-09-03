#!/usr/bin/env python3
"""
vision/eval/sweep.py
────────────────────
녹화된 EAR 로그에 임계값을 스윕해 **깜빡임 검출 정확도**를 냅니다.
(회의 항목 1: "눈깜박임 수 측정 및 정확도 분석")

    python vision/eval/sweep.py vision/eval/data/S01.jsonl
    python vision/eval/sweep.py data/*.jsonl --top 15
    python vision/eval/sweep.py --self-test        # 합성 신호로 스윕기 검증

정답(cue)과 검출을 ±TOL 초 안에서 1:1 매칭해 TP / FP / FN 을 셉니다.

이 결과가 있어야 ear.py 의 EAR_CLOSED / EAR_OPEN 이 추정치가 아니라
실측 기반이 됩니다. 지금 값(0.21 / 0.25)은 문헌에서 흔히 쓰는 값일 뿐
이 카메라·이 피험자에 맞춘 값이 아닙니다.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

TOL = 0.6          # 정답과 검출을 같은 사건으로 볼 시간 허용치 (초)

MIN_MS, MAX_MS = 60, 500


def detect(frames, closed_th, open_th, min_ms=MIN_MS, max_ms=MAX_MS):
    """EAR 시계열 → 깜빡임 종료 시각 리스트. ear.py 와 같은 상태기계입니다."""
    out, closed_since = [], None
    for t, ear in frames:
        if ear is None:
            continue
        if closed_since is None:
            if ear < closed_th:
                closed_since = t
        elif ear > open_th:
            ms = (t - closed_since) * 1000.0
            if min_ms <= ms <= max_ms:
                out.append(t)
            closed_since = None
    return out


def match(cues, dets, tol=TOL):
    """탐욕적 1:1 매칭. 정답 하나에 검출 하나."""
    used = [False] * len(dets)
    tp = 0
    for c in cues:
        best, bd = -1, tol
        for i, d in enumerate(dets):
            if used[i]:
                continue
            gap = abs(d - c)
            if gap <= bd:
                best, bd = i, gap
        if best >= 0:
            used[best] = True
            tp += 1
    return tp, len(dets) - tp, len(cues) - tp      # TP, FP, FN


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def load(paths):
    frames, cues, metas = [], [], []
    for path in paths:
        base = None
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            if o["type"] == "meta":
                metas.append(o)
            elif o["type"] == "frame":
                if base is None:
                    base = o["t"]
                frames.append((o["t"], o["ear"]))
            elif o["type"] == "cue":
                cues.append(o["t"])
    return frames, cues, metas


def synth():
    """합성 신호: 60초 동안 4초마다 200ms 감음 → 정답 14개."""
    frames, cues, t = [], [], 0.0
    for i in range(600):
        closed = (i % 40) in (0, 1)
        frames.append((t, 0.10 if closed else 0.30))
        if i % 40 == 0 and i > 0:
            cues.append(t + 0.2)
        t += 0.1
    return frames, cues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        frames, cues, metas = *synth(), []
        print("합성 신호 자체검증 (정답 14개)\n")
    else:
        files = [p for pat in args.paths for p in glob.glob(pat)]
        if not files:
            sys.exit("녹화 파일이 없습니다.  먼저: python vision/eval/record.py --guided --subject S01")
        frames, cues, metas = load(files)
        for m in metas:
            print(f"{m['subject']:6}  안경={'예' if m.get('glasses') else '아니오'}  "
                  f"{'신호' if m.get('guided') else '수동'}  {m.get('recorded_at','')}")
        print()

    if not cues:
        sys.exit("정답(cue)이 없습니다. --guided 로 다시 녹화하세요.")
    valid = [e for _, e in frames if e is not None]
    print(f"프레임 {len(frames)} (얼굴 검출 {len(valid)}, "
          f"{100*len(valid)/max(len(frames),1):.0f}%)   정답 {len(cues)}\n")

    rows = []
    for c10 in range(14, 29):                       # closed 0.14 ~ 0.28
        for gap10 in range(1, 9):                   # open = closed + 0.01~0.08
            ct, ot = c10 / 100, (c10 + gap10) / 100
            tp, fp, fn = match(cues, detect(frames, ct, ot))
            p, r, f = prf(tp, fp, fn)
            rows.append((f, p, r, tp, fp, fn, ct, ot))

    rows.sort(reverse=True)
    print(f"{'F1':>6} {'정밀도':>7} {'재현율':>7} {'TP':>4} {'FP':>4} {'FN':>4}   "
          f"{'CLOSED':>7} {'OPEN':>6}")
    print("─" * 60)
    for f, p, r, tp, fp, fn, ct, ot in rows[:args.top]:
        print(f"{f:6.3f} {p:7.3f} {r:7.3f} {tp:4d} {fp:4d} {fn:4d}   {ct:7.2f} {ot:6.2f}")

    best = rows[0]
    print(f"\n최적:  EAR_CLOSED = {best[6]:.2f}   EAR_OPEN = {best[7]:.2f}   "
          f"(F1 {best[0]:.3f})")
    print("vision/blink/ear.py 의 상수를 이 값으로 바꾸세요.")
    if len(metas) > 1:
        print("\n주의: 여러 피험자를 합쳐 스윕했습니다. 한 사람에게 과적합되지 않았는지")
        print("      피험자별로도 따로 돌려 비교하세요.")


if __name__ == "__main__":
    main()
