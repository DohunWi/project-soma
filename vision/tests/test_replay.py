"""
vision/eval/replay.py 테스트. 카메라·녹화 파일 없이 합성 신호로 돌립니다.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "eval"))
from replay import (blink_series, load_frames, run_fusion,  # noqa: E402
                    summarize, synth_frames)


def test_합성신호를_다시_돌리면_깜빡임이_잡힌다():
    series = blink_series(synth_frames(seconds=90.0, fatigue=False))
    stat = summarize(series)
    assert stat["n"] > 100
    assert 12.0 <= stat["avg"] <= 18.0        # 4초마다 = 분당 15회


def test_피로_신호는_빈도가_떨어진다():
    series = blink_series(synth_frames(fatigue=True))
    rates = [r for _, r in series if r is not None]
    assert rates[0] > rates[-1]


def test_임계를_낮추면_깜빡임을_덜_센다():
    """임계는 평소 EAR 대비 비율입니다. 비율을 낮추면 더 깊이 감아야 인정합니다."""
    frames = synth_frames(fatigue=False)
    normal = summarize(blink_series(frames, closed=0.60, open_th=0.75))
    strict = summarize(blink_series(frames, closed=0.20, open_th=0.30))
    assert normal["avg"] > 0
    assert strict.get("avg", 0) < normal["avg"]


def test_의자_합성값이_불균형_알림을_만들지_않는다():
    """압력 네 채널에 좌우 차이를 주면 imbalance 가 300초에서 CAUTION 을 만듭니다.
    깜빡임 축을 분리해서 보려고 만든 도구인데 다른 축이 섞이면 쓸 수 없습니다."""
    series = blink_series(synth_frames(fatigue=False), repeat=8)   # 12분 이상
    res = run_fusion(series)
    assert res["duration_sec"] > 600
    assert res["alerts"]["CAUTION"] == 0
    assert res["alerts"]["DANGER"] == 0


def test_임계를_올리면_알림이_늘어난다():
    series = blink_series(synth_frames(fatigue=True), repeat=20)
    loose = run_fusion(series, low=6.0, ok=8.0)
    tight = run_fusion(series, low=10.0, ok=13.0)
    assert tight["low_blink_sec"] > loose["low_blink_sec"]
    assert (tight["alerts"]["CAUTION"] + tight["alerts"]["DANGER"]) >= \
           (loose["alerts"]["CAUTION"] + loose["alerts"]["DANGER"])


def test_fusion_상수를_되돌려_놓는다():
    """분석 도구가 판정 모듈의 상수를 바꿔 놓고 나가면 안 됩니다."""
    sys.path.insert(0, str(ROOT / "fusion"))
    import state as fusion
    before = (fusion.BLINK_RATE_LOW, fusion.BLINK_RATE_OK)
    run_fusion(blink_series(synth_frames(seconds=20.0)), low=99.0, ok=100.0)
    assert (fusion.BLINK_RATE_LOW, fusion.BLINK_RATE_OK) == before


def test_녹화파일_형식을_읽는다(tmp_path):
    p = tmp_path / "S99.jsonl"
    lines = [{"type": "meta", "subject": "S99"},
             {"type": "frame", "t": 100.0, "ear": 0.30, "face_width_px": 200.0},
             {"type": "frame", "t": 100.1, "ear": None, "face_width_px": None},
             {"type": "cue", "t": 100.2},
             {"type": "frame", "t": 100.2, "ear": 0.10, "face_width_px": 201.0}]
    p.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    frames = load_frames([str(p)])
    assert len(frames) == 2                    # ear 가 None 인 프레임은 빠집니다
    assert frames[0][0] == 0.0                 # 시각은 0 부터
