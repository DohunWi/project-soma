"""fusion/state.py 테스트. 하드웨어 없이 돌아갑니다.  pytest fusion/"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state import FusionState, step, LOW_BLINK_CAUTION, STATIC_CAUTION  # noqa: E402

SEATED = [900, 700, 1000, 910]
EMPTY  = [1, 1, 1, 1]


def run(samples, t0=1000.0, dt=1.0):
    st, d = FusionState(), None
    t = t0
    for s in samples:
        st, d = step(st, s, t)
        t += dt
    return st, d


def sample(pressure=None, blink_rate=15.0, dist=60.0, jitter=0):
    p = list(pressure or SEATED)
    if jitter:
        p = [v + jitter for v in p]
    return {"pressure": p, "blink_rate": blink_rate,
            "face_distance_cm": dist, "face_detected": True, "user_name": "t"}


def test_정상():
    _, d = run([sample() for _ in range(10)])
    assert d["state"] == "NORMAL"
    assert d["reasons"] == []


def test_자리비움():
    _, d = run([sample(pressure=EMPTY)])
    assert d["state"] == "ABSENT"


def test_저깜빡임이_주의를_만든다():
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample(blink_rate=5.0) for _ in range(n)])
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
