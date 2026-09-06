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


# ── 평소값 대비 판정 ─────────────────────────────────────────────────────────

def sample_rel(rate, dist=None, base_rate=21.7, base_dist=35.0):
    s = {"pressure": SEATED, "face_detected": True, "user_name": "t",
         "blink_rate": rate, "blink_rate_baseline": base_rate,
         "face_distance_baseline_cm": base_dist}
    if dist is not None:
        s["face_distance_cm"] = dist
    return s


def test_평소가_높으면_절대임계_위에서도_저깜빡임이다():
    """평소 21.7 인 사람의 11회/분은 절반입니다. 절대 임계 8 로는 아무 일도 없습니다."""
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample_rel(11.0) for _ in range(n)])
    assert d["state"] == "CAUTION"
    assert "low_blink" in d["reasons"]


def test_평소가_낮으면_같은_값이_정상이다():
    """평소 12회인 사람에게 11회는 정상입니다. 같은 숫자가 사람마다 다릅니다."""
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample_rel(11.0, base_rate=12.0) for _ in range(n)])
    assert "low_blink" not in d["reasons"]


def test_평소값이_없으면_절대임계를_쓴다():
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample(blink_rate=11.0) for _ in range(n)])       # baseline 없음
    assert "low_blink" not in d["reasons"]                        # 8 이상이므로 정상
    _, d2 = run([sample(blink_rate=5.0) for _ in range(n)])
    assert "low_blink" in d2["reasons"]


def test_회복_임계도_평소_대비다():
    st, _ = run([sample_rel(11.0) for _ in range(200)])
    assert st.low_blink_sec > 100
    t = 1000.0 + 200
    for _ in range(3):
        st, _ = step(st, sample_rel(18.0), t)     # 21.7 의 83% → 회복
        t += 1
    assert st.low_blink_sec == 0.0


def test_평소_35cm_인_사람은_35cm_에서_경고받지_않는다():
    """절대 임계 45cm 로는 앉는 순간부터 계속 근접 경고였습니다."""
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample_rel(20.0, dist=35.0) for _ in range(n)])
    assert "close_distance" not in d["reasons"]


def test_평소보다_다가오면_근접이다():
    n = int(LOW_BLINK_CAUTION) + 5
    _, d = run([sample_rel(20.0, dist=28.0) for _ in range(n)])   # 35 의 80%
    assert "close_distance" in d["reasons"]


def test_거리_평소값이_없으면_절대임계를_쓴다():
    n = int(LOW_BLINK_CAUTION) + 5
    s = sample(blink_rate=20.0, dist=40.0)
    _, d = run([s for _ in range(n)])
    assert "close_distance" in d["reasons"]        # 45cm 미만
