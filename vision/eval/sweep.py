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

# 감은 시간 게이트도 스윕합니다. 이전에는 EAR 임계 두 개만 스윕하고 이 값은
# 60/500 으로 고정이었는데, 이 축이 정확도를 크게 바꿉니다:
#   min_ms 를 올리면 안경 반사가 만드는 짧은 오검출이 걸러집니다
#   max_ms 를 내리면 "감고 있는 것" 을 깜빡임으로 세지 않습니다
# 둘 다 안경 착용자에서 특히 다르게 잡힙니다.
MIN_MS_GRID = (40, 60, 80, 100)
MAX_MS_GRID = (300, 400, 500, 700)


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
    ap.add_argument("--fast", action="store_true",
                    help="EAR 임계만 스윕 (감은 시간 게이트는 60/500 고정)")
    ap.add_argument("--json", help="결과를 파일로 저장. 피험자별 비교에 씁니다")
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

    min_grid = (MIN_MS,) if args.fast else MIN_MS_GRID
    max_grid = (MAX_MS,) if args.fast else MAX_MS_GRID

    rows = []
    for c10 in range(14, 29):                       # closed 0.14 ~ 0.28
        for gap10 in range(1, 9):                   # open = closed + 0.01~0.08
            ct, ot = c10 / 100, (c10 + gap10) / 100
            for mn in min_grid:
                for mx in max_grid:
                    tp, fp, fn = match(cues, detect(frames, ct, ot, mn, mx))
                    p, r, f = prf(tp, fp, fn)
                    rows.append({"f1": f, "precision": p, "recall": r,
                                 "tp": tp, "fp": fp, "fn": fn,
                                 "closed": ct, "open": ot,
                                 "min_ms": mn, "max_ms": mx})

    # F1 이 같으면 재현율이 높은 쪽을 위로. 놓친 깜빡임(FN)이 오검출(FP)보다
    # 나쁩니다 — 저깜빡임 판정이 "적게 깜빡였다" 를 세는 일이기 때문입니다.
    rows.sort(key=lambda r: (r["f1"], r["recall"]), reverse=True)
    print(f"{'F1':>6} {'정밀도':>7} {'재현율':>7} {'TP':>4} {'FP':>4} {'FN':>4}   "
          f"{'CLOSED':>7} {'OPEN':>6} {'MIN_MS':>7} {'MAX_MS':>7}")
    print("─" * 78)
    for r in rows[:args.top]:
        print(f"{r['f1']:6.3f} {r['precision']:7.3f} {r['recall']:7.3f} "
              f"{r['tp']:4d} {r['fp']:4d} {r['fn']:4d}   "
              f"{r['closed']:7.2f} {r['open']:6.2f} {r['min_ms']:7d} {r['max_ms']:7d}")

    best = rows[0]
    print(f"\n최적:  EAR_CLOSED = {best['closed']:.2f}   EAR_OPEN = {best['open']:.2f}   "
          f"MIN_CLOSED_MS = {best['min_ms']}   MAX_CLOSED_MS = {best['max_ms']}   "
          f"(F1 {best['f1']:.3f})")
    print("vision/blink/ear.py 의 상수를 이 값으로 바꾸세요.")
    print(f"조합 {len(rows)}개를 봤습니다"
          + ("  (--fast: EAR 임계만)" if args.fast else ""))

    if args.json:
        payload = {"subjects": [m.get("subject") for m in metas],
                   "glasses": [m.get("glasses") for m in metas],
                   "frames": len(frames), "cues": len(cues),
                   "best": best, "top": rows[:args.top]}
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"저장: {args.json}")
    if len(metas) > 1:
        print("\n주의: 여러 피험자를 합쳐 스윕했습니다. 한 사람에게 과적합되지 않았는지")
        print("      피험자별로도 따로 돌려 비교하세요.")


if __name__ == "__main__":
    main()
