"""fusion/state.py 테스트. 하드웨어 없이 돌아갑니다.  pytest fusion/"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion.config import DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING  # noqa: E402
from fusion.state import FusionState, step  # noqa: E402

SEATED = [900, 700, 1000, 910]
EMPTY  = [1, 1, 1, 1]


def run(samples, t0=1000.0, dt=1.0, timing=DEMO_FUSION_TIMING):
    st, d = FusionState(), None
    t = t0
    for s in samples:
        st, d = step(st, s, t, timing=timing)
        t += dt
    return st, d


def run_elapsed(builder, elapsed, timing=DEMO_FUSION_TIMING, t0=1000.0):
    """Feed one sample per second, including both endpoints."""
    st, decision = FusionState(), None
    for offset in range(int(elapsed) + 1):
        st, decision = step(
            st,
            builder(offset),
            t0 + offset,
            timing=timing,
        )
    return st, decision


def sample(pressure=None, blink_rate=15.0, dist=60.0, jitter=0):
    p = list(pressure or SEATED)
    if jitter:
        p = [v + jitter for v in p]
    return {"pressure": p, "blink_rate": blink_rate,
            "face_distance_cm": dist, "face_detected": True, "user_name": "t"}


def moving_sample(offset, *, blink_rate=15.0, dist=60.0):
    pressure = [850 + (100 if offset % 2 else 0)] * 4
    return sample(pressure=pressure, blink_rate=blink_rate, dist=dist)


def test_정상():
    _, d = run([sample() for _ in range(10)])
    assert d["state"] == "NORMAL"
    assert d["reasons"] == []


def test_자리비움():
    _, d = run([sample(pressure=EMPTY)])
    assert d["state"] == "ABSENT"


def test_저깜빡임이_주의를_만든다():
    _, d = run_elapsed(
        lambda offset: moving_sample(offset, blink_rate=5.0),
        NORMAL_FUSION_TIMING.low_blink_caution_sec,
        timing=NORMAL_FUSION_TIMING,
    )
    assert d["state"] == "CAUTION"
    assert "low_blink" in d["reasons"]


def test_깜빡임_회복하면_리셋된다():
    st, _ = run([sample(blink_rate=5.0) for _ in range(200)])
    assert st.low_blink_sec > 100
    t = 1000.0 + 200
    for _ in range(3):
        st, d = step(st, sample(blink_rate=15.0), t)
        t += 1
    assert st.low_blink_sec == 0.0


def test_히스테리시스_중간값은_유지된다():
    st, _ = run([sample(blink_rate=5.0) for _ in range(100)])
    before = st.low_blink_sec
    st, _ = step(st, sample(blink_rate=9.5), 1000.0 + 100)   # 8~11 사이
    assert st.low_blink_sec == before


def test_자리비움이_누적을_리셋한다():
    st, _ = run([sample(blink_rate=5.0) for _ in range(200)])
    assert st.low_blink_sec > 0
    st, d = step(st, {"pressure": EMPTY}, 1000.0 + 200)
    assert d["state"] == "ABSENT"
    assert st.low_blink_sec == 0.0
    assert st.static_hold_sec == 0.0


def test_움직이면_정적타이머가_리셋된다():
    st, _ = run([sample() for _ in range(300)])
    assert st.static_hold_sec > 200
    st, _ = step(st, sample(jitter=200), 1000.0 + 300)       # 큰 압력 변화
    assert st.static_hold_sec == 0.0


def test_정적_누적은_리셋되지_않는다():
    st, _ = run([sample() for _ in range(300)])
    total = st.static_total
    st, _ = step(st, sample(jitter=200), 1000.0 + 300)
    assert st.static_hold_sec == 0.0
    assert st.static_total >= total


def test_시계_점프는_누적하지_않는다():
    st, _ = run([sample() for _ in range(10)])
    before = st.static_hold_sec
    st, _ = step(st, sample(), 1000.0 + 10 + 3600)           # 1시간 점프
    assert st.static_hold_sec == before


def test_웹캠_없으면_신뢰도가_낮다():
    _, d = run([{"pressure": SEATED, "user_name": "t"}])
    assert d["confidence"] < 0.5


def test_좌우편중_히스테리시스():
    st, _ = run([sample(pressure=[1000, 700, 1000, 700]) for _ in range(5)])
    assert st.balance == "LEFT"
    st, _ = step(st, sample(pressure=[900, 860, 900, 860]), 1005.0)   # 차이 80 > 30
    assert st.balance == "LEFT"                                        # 아직 유지
    st, _ = step(st, sample(pressure=[900, 890, 900, 890]), 1006.0)   # 차이 20 < 30
    assert st.balance == "CENTER"


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(9, "NORMAL"), (10, "CAUTION"), (20, "DANGER")],
)
def test_demo_static_elapsed_boundaries(elapsed, expected):
    _, decision = run_elapsed(
        lambda _offset: sample(pressure=[850, 850, 850, 850]),
        elapsed,
    )
    assert decision["state"] == expected


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(9, "NORMAL"), (10, "CAUTION")],
)
def test_demo_imbalance_elapsed_boundaries(elapsed, expected):
    pressure = [1000, 700, 1000, 700]
    _, decision = run_elapsed(lambda _offset: sample(pressure=pressure), elapsed)
    assert decision["state"] == expected


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(1199, "NORMAL"), (1200, "CAUTION"), (2700, "DANGER")],
)
def test_normal_static_threshold_regression(elapsed, expected):
    _, decision = run_elapsed(
        lambda _offset: sample(pressure=[850, 850, 850, 850]),
        elapsed,
        timing=NORMAL_FUSION_TIMING,
    )
    assert decision["state"] == expected


@pytest.mark.parametrize(
    ("timing", "elapsed", "expected"),
    [
        (DEMO_FUSION_TIMING, 9, "NORMAL"),
        (DEMO_FUSION_TIMING, 10, "CAUTION"),
        (DEMO_FUSION_TIMING, 20, "DANGER"),
        (NORMAL_FUSION_TIMING, 299, "NORMAL"),
        (NORMAL_FUSION_TIMING, 300, "CAUTION"),
        (NORMAL_FUSION_TIMING, 900, "DANGER"),
    ],
)
def test_low_blink_profile_boundaries(timing, elapsed, expected):
    _, decision = run_elapsed(
        lambda offset: moving_sample(offset, blink_rate=5.0),
        elapsed,
        timing=timing,
    )
    assert decision["state"] == expected
    if expected != "NORMAL":
        assert "low_blink" in decision["reasons"]


@pytest.mark.parametrize(
    ("timing", "elapsed", "expected"),
    [
        (DEMO_FUSION_TIMING, 9, "NORMAL"),
        (DEMO_FUSION_TIMING, 10, "CAUTION"),
        (NORMAL_FUSION_TIMING, 299, "NORMAL"),
        (NORMAL_FUSION_TIMING, 300, "CAUTION"),
    ],
)
def test_close_distance_profile_boundaries(timing, elapsed, expected):
    _, decision = run_elapsed(
        lambda offset: moving_sample(offset, dist=40.0),
        elapsed,
        timing=timing,
    )
    assert decision["state"] == expected
    if expected == "CAUTION":
        assert "close_distance" in decision["reasons"]


@pytest.mark.parametrize("timing", [DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING])
def test_absent_is_immediate_for_every_profile(timing):
    _, decision = run_elapsed(
        lambda _offset: sample(pressure=EMPTY),
        0,
        timing=timing,
    )
    assert decision["state"] == "ABSENT"


@pytest.mark.parametrize("timing", [DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING])
def test_max_gap_accepts_five_seconds_but_not_more(timing):
    st, _ = step(FusionState(), sample(), 1000.0, timing=timing)
    st, _ = step(st, sample(), 1005.0, timing=timing)
    assert st.static_hold_sec == 5.0

    st, _ = step(st, sample(), 1010.001, timing=timing)
    assert st.static_hold_sec == 5.0


@pytest.mark.parametrize("timing", [DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING])
def test_balance_hysteresis_is_profile_independent(timing):
    st, _ = step(
        FusionState(),
        sample(pressure=[1000, 700, 1000, 700]),
        1000.0,
        timing=timing,
    )
    st, _ = step(
        st,
        sample(pressure=[900, 860, 900, 860]),
        1001.0,
        timing=timing,
    )
    assert st.balance == "LEFT"

    st, _ = step(
        st,
        sample(pressure=[900, 890, 900, 890]),
        1002.0,
        timing=timing,
    )
    assert st.balance == "CENTER"


@pytest.mark.parametrize("timing", [DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING])
def test_vision_hysteresis_is_profile_independent(timing):
    st, _ = step(
        FusionState(),
        moving_sample(0, blink_rate=5.0, dist=40.0),
        1000.0,
        timing=timing,
    )
    st, _ = step(
        st,
        moving_sample(1, blink_rate=5.0, dist=40.0),
        1001.0,
        timing=timing,
    )
    assert st.low_blink_sec == 1.0
    assert st.close_dist_sec == 1.0

    st, _ = step(
        st,
        moving_sample(2, blink_rate=9.5, dist=47.0),
        1002.0,
        timing=timing,
    )
    assert st.low_blink_sec == 1.0
    assert st.close_dist_sec == 1.0

    st, _ = step(
        st,
        moving_sample(3, blink_rate=11.0, dist=50.0),
        1003.0,
        timing=timing,
    )
    assert st.low_blink_sec == 0.0
    assert st.close_dist_sec == 0.0


def test_웹캠_값이_없으면_metrics_에서_키를_뺀다():
    """null 은 state 계약 위반입니다. 대시보드는 없는 키를 '—' 로 표시합니다."""
    _, d = run([{"pressure": SEATED, "user_name": "t"}])
    assert "blink_rate" not in d["metrics"]
    assert "face_distance_cm" not in d["metrics"]
    assert None not in d["metrics"].values()


def test_baseline_과_검출률을_그대로_넘긴다():
    s = sample()
    s.update({"blink_rate_baseline": 15.2, "face_distance_baseline_cm": 60.0,
              "detect_rate": 0.9})
    _, d = run([s])
    m = d["metrics"]
    assert m["blink_rate_baseline"] == 15.2
    assert m["face_distance_baseline_cm"] == 60.0
    assert m["detect_rate"] == 0.9


def test_검출률이_낮으면_신뢰도가_낮다():
    """프레임 하나가 우연히 잡힌 것과 계속 잡히는 것을 구분합니다."""
    good = sample(); good["detect_rate"] = 1.0
    poor = sample(); poor["detect_rate"] = 0.1
    _, dg = run([good])
    _, dp = run([poor])
    assert dg["confidence"] > dp["confidence"]
    assert dp["confidence"] < 0.55
