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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "blink"))
from ear import detect_blinks  # noqa: E402

# 정답(신호)과 검출을 같은 사건으로 볼 시간 창 (초).
#
# **대칭이 아닙니다.** 깜빡임은 신호를 본 뒤에 일어나므로 지연은 항상 양수입니다.
#   반응 시간(0.2~0.4초) + 눈을 감았다 뜨는 시간(0.1~0.3초, 검출은 뜨는 순간)
#   + 프레임 간격(15fps 면 0.07초)
# 실측 S01 에서 지연 중앙값이 +0.73초였습니다. ±0.6 대칭 창을 쓰면 맞게 잡은
# 검출이 거의 전부 미검출로 기록됩니다 — 재현율 0.158 이 그렇게 나왔습니다.
# 음수쪽은 프레임 흔들림만 흡수할 만큼만 둡니다.
CUE_WINDOW = (-0.3, 1.5)
TOL = CUE_WINDOW           # 이전 이름 유지

MIN_MS, MAX_MS = 60, 500

# 감은 시간 게이트도 스윕합니다. 이전에는 EAR 임계 두 개만 스윕하고 이 값은
# 60/500 으로 고정이었는데, 이 축이 정확도를 크게 바꿉니다:
#   min_ms 를 올리면 안경 반사가 만드는 짧은 오검출이 걸러집니다
#   max_ms 를 내리면 "감고 있는 것" 을 깜빡임으로 세지 않습니다
# 둘 다 안경 착용자에서 특히 다르게 잡힙니다.
MIN_MS_GRID = (40, 60, 80, 100)
MAX_MS_GRID = (300, 400, 500, 700)


def detect(frames, r_closed, r_open, min_ms=MIN_MS, max_ms=MAX_MS):
    """
    EAR 시계열 → 깜빡임 종료 시각.

    **런타임과 같은 구현(ear.detect_blinks)을 씁니다.** 예전에는 이 파일이
    상태기계를 따로 갖고 있었는데, ear.py 에 baseline 추정이 들어오면서
    둘이 갈라졌습니다. 분석이 실제 동작과 다르면 그 표는 아무것도 증명하지
    못합니다.

    임계는 절대값이 아니라 **평소 EAR 대비 비율**입니다 (ear.py 참조).
    """
    return detect_blinks(frames, r_closed=r_closed, r_open=r_open,
                         min_ms=min_ms, max_ms=max_ms)


def dip_reference(frames, win_sec=2.0, ratio=0.55, fps=15.0):
    """
    **임계와 무관한 기준 깜빡임 수.** 국소 baseline 대비 골의 깊이만 봅니다.

    자연 깜빡임에는 정답이 없습니다. 신호 방식은 의도적 깜빡임만 만들고,
    그것에 맞춰 임계를 고르면 얕고 빠른 자연 깜빡임을 놓칩니다.
    실제로 신호 녹화만으로 스윕하면 R_CLOSED=0.40 이 뽑히는데, 그 값으로는
    자연 깜빡임이 절반도 안 잡힙니다.

    이 함수는 EAR 곡선에서 "앞뒤 2초의 평소값 대비 55% 아래로 내려간 구간"
    을 셉니다. 신호 녹화에서 검증했습니다 — 정답 19개에 골 18개.
    """
    ts = [t for t, _ in frames]
    es = [e for _, e in frames]
    n = len(frames)
    half = max(int(win_sec * fps), 5)
    out, i = [], 0
    # 2단계입니다. 넓게(90%) 잘라 하나의 골로 묶고, 그 골의 **최저점**이
    # 깊은지(55%) 봅니다. 55% 로 바로 자르면 임계 근처에서 흔들리는 한 번의
    # 깜빡임이 여러 개로 쪼개집니다 — 자연 녹화에서 21개가 36개로 부풀었습니다.
    while i < n:
        lo, hi = max(0, i - half), min(n, i + half)
        local = sorted(es[lo:hi])
        upper = local[len(local) // 2:]
        base = upper[len(upper) // 2] if upper else 0.0
        if base > 0 and es[i] < base * 0.9:
            j = i
            while j + 1 < n and es[j + 1] < base * 0.9:
                j += 1
            if min(es[i:j + 1]) < base * ratio:
                out.append(ts[i])
            i = j + 1
        else:
            i += 1
    return out


def match(cues, dets, window=CUE_WINDOW):
    """탐욕적 1:1 매칭. 정답 하나에 검출 하나. (TP, FP, FN)"""
    tp, used, _ = _match_flags(cues, dets, window)
    return tp, len(dets) - tp, len(cues) - tp


def _match_flags(cues, dets, window=CUE_WINDOW):
    """매칭된 검출이 어느 것인지와 지연 목록도 돌려줍니다."""
    lo, hi = window
    used = [False] * len(dets)
    tp, lags = 0, []
    for c in cues:
        best, bd = -1, None
        for i, d in enumerate(dets):
            if used[i]:
                continue
            lag = d - c
            if lo <= lag <= hi and (bd is None or lag < bd):
                best, bd = i, lag
        if best >= 0:
            used[best] = True
            lags.append(round(bd, 3))
            tp += 1
    return tp, used, lags


def score(cues, dets, holds, span, window=CUE_WINDOW):
    """
    신호 방식의 채점.

    **짝 없는 검출을 전부 오검출로 세면 안 됩니다.** 사람은 3~4초에 한 번
    무의식적으로 깜빡이고 신호 간격도 4초라, 신호 사이의 자연 깜빡임이
    전부 오검출로 잡힙니다. 검출기가 맞게 잡은 것을 틀렸다고 세는 셈입니다.

    그래서 셋으로 나눕니다.
      TP/FN        신호에 맞춘 깜빡임을 잡았는가 → 재현율
      FP           "눈 뜨고 버티기" 구간의 검출 → 진짜 오검출
      unlabeled    나머지 구간의 짝 없는 검출 → 자연 깜빡임 + 오검출이 섞인 값.
                   생리적 정상치(분당 15~20회)와 비교해서 읽습니다
    """
    tp, used, lags = _match_flags(cues, dets, window)
    hold_sec = sum(max(0.0, e - s) for s, e in holds)
    free_sec = max(span - hold_sec, 1e-9)

    fp = unlabeled = 0
    for i, d in enumerate(dets):
        if used[i]:
            continue
        if any(s <= d <= e for s, e in holds):
            fp += 1
        else:
            unlabeled += 1

    recall = tp / len(cues) if cues else 0.0
    lag_median = round(sorted(lags)[len(lags) // 2], 2) if lags else None
    return {"tp": tp, "fn": len(cues) - tp, "recall": recall, "lag_median": lag_median,
            "fp": fp, "hold_sec": round(hold_sec, 1),
            "fp_per_min": round(fp * 60.0 / hold_sec, 2) if hold_sec > 0 else None,
            "unlabeled": unlabeled,
            "unlabeled_per_min": round(unlabeled * 60.0 / free_sec, 1)}


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def load(paths):
    frames, cues, metas, holds = [], [], [], []
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
            elif o["type"] == "hold":
                holds.append((o["start"], o["end"]))
    return frames, cues, metas, holds


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
    ap.add_argument("--natural", help="자연 깜빡임 녹화(신호 없음). 임계가 얕은 "
                                      "깜빡임까지 잡는지 확인합니다")
    args = ap.parse_args()

    if args.self_test:
        frames, cues = synth()
        metas, holds = [], []
        print("합성 신호 자체검증 (정답 14개)\n")
    else:
        files = [p for pat in args.paths for p in glob.glob(pat)]
        if not files:
            sys.exit("녹화 파일이 없습니다.  먼저: python vision/eval/record.py --guided --subject S01")
        frames, cues, metas, holds = load(files)
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

    span = (frames[-1][0] - frames[0][0]) if frames else 0.0

    # 자연 녹화가 있으면 "얕은 깜빡임까지 잡는가" 를 같이 봅니다.
    nat_frames, nat_span, nat_ref = None, 0.0, None
    if args.natural:
        nat_frames, _, _, _ = load([args.natural])
        nat_span = nat_frames[-1][0] - nat_frames[0][0]
        nat_ref = len(dip_reference(nat_frames)) * 60.0 / nat_span
        print(f"자연 녹화 {nat_span:.0f}초 — 골 기준 {nat_ref:.1f}회/분\n")

    rows = []
    for c100 in range(40, 81, 5):                   # R_CLOSED 0.40 ~ 0.80
        for gap100 in (5, 10, 15, 20, 25):          # R_OPEN = R_CLOSED + 0.05~0.25
            ct, ot = c100 / 100, (c100 + gap100) / 100
            for mn in min_grid:
                for mx in max_grid:
                    dets = detect(frames, ct, ot, mn, mx)
                    row = score(cues, dets, holds, span)
                    if nat_frames is not None:
                        nd = detect(nat_frames, ct, ot, mn, mx)
                        row["natural_per_min"] = round(len(nd) * 60.0 / nat_span, 1)
                        row["natural_gap"] = abs(row["natural_per_min"] - nat_ref)
                    row.update({"closed": ct, "open": ot, "min_ms": mn, "max_ms": mx})
                    rows.append(row)

    # 재현율이 먼저입니다. 저깜빡임 판정은 "적게 깜빡였다" 를 세는 일이라
    # 놓친 깜빡임이 곧 잘못된 경고로 이어집니다. 같은 재현율이면
    # 버티기 구간의 오검출이 적은 쪽을 위로 둡니다.
    # 재현율이 먼저, 그다음 자연 깜빡임 기준과의 차이, 그다음 오검출.
    # 자연 녹화가 없으면 예전 기준으로 돌아갑니다.
    rows.sort(key=lambda r: (r["recall"], -r.get("natural_gap", 0.0),
                             -(r["fp_per_min"] or 0.0), -r["unlabeled_per_min"]),
              reverse=True)

    if holds:
        print(f"버티기 구간 {rows[0]['hold_sec']:.0f}초 — 여기서 잡힌 검출만 오검출입니다\n")
    else:
        print("버티기 구간이 없는 녹화입니다. 오검출(FP)을 신뢰할 수 없습니다.\n"
              "  --guided 로 다시 찍으면 앞 15초가 오검출 측정 구간이 됩니다.\n")

    nat_col = f"{'자연/분':>8}" if nat_ref is not None else ""
    print(f"{'재현율':>7} {'TP':>4} {'FN':>4} {'오검출/분':>10}{nat_col}   "
          f"{'R_CLOSED':>9} {'R_OPEN':>7} {'MIN_MS':>7} {'MAX_MS':>7}")
    print("─" * 88)
    for r in rows[:args.top]:
        fpm = "—" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"
        nat = f"{r['natural_per_min']:8.1f}" if nat_ref is not None else ""
        print(f"{r['recall']:7.3f} {r['tp']:4d} {r['fn']:4d} {fpm:>10}{nat}   "
              f"{r['closed']:9.2f} {r['open']:7.2f} {r['min_ms']:7d} {r['max_ms']:7d}")
    print("\n라벨없음/분 = 신호 사이의 짝 없는 검출. 자연 깜빡임(정상 15~20회/분)과"
          "\n오검출이 섞인 값입니다. 이 값이 20 을 크게 넘으면 오검출을 의심하세요.")

    best = rows[0]
    fpm = "측정 불가" if best["fp_per_min"] is None else f"{best['fp_per_min']:.2f}/분"
    print(f"\n최적:  R_CLOSED = {best['closed']:.2f}   R_OPEN = {best['open']:.2f}   "
          f"MIN_CLOSED_MS = {best['min_ms']}   MAX_CLOSED_MS = {best['max_ms']}")
    print(f"       재현율 {best['recall']:.3f}   오검출 {fpm}   "
          f"라벨없음 {best['unlabeled_per_min']:.1f}/분")
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
