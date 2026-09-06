"""
vision/blink/ear.py 테스트. 카메라 없이 EAR 시계열을 합성해서 돌립니다.

    pytest vision/
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "blink"))
from ear import (BlinkCounter, MIN_OBSERVED_SEC, eye_ear,  # noqa: E402
                 EAR_CLOSED, EAR_OPEN)

OPEN_EAR, CLOSED_EAR = 0.30, 0.10


def feed(c, seconds, period_sec, closed_sec=0.2, step=0.05, t0=0.0):
    """period_sec 마다 closed_sec 동안 눈을 감는 신호를 넣습니다."""
    t = t0
    while t < t0 + seconds:
        phase = (t - t0) % period_sec
        c.update(CLOSED_EAR if phase < closed_sec else OPEN_EAR, t)
        t += step
    return t


def test_관측이_짧으면_rate를_내보내지_않는다():
    c = BlinkCounter()
    now = feed(c, seconds=MIN_OBSERVED_SEC - 2, period_sec=3.0)
    assert c.rate(now) is None          # None → 받는 쪽이 누적을 멈추고 리셋하지 않습니다
    assert c.count(now) >= 1            # 세기는 세고 있습니다


def test_창이_덜_찼으면_관측시간으로_환산한다():
    """이전 구현은 항상 60 으로 나눠 실제 20회/분을 5.0 으로 보고했습니다."""
    c = BlinkCounter()
    now = feed(c, seconds=15.0, period_sec=3.0)     # 3초에 한 번 = 분당 20회
    rate = c.rate(now)
    assert rate is not None
    assert 15.0 <= rate <= 25.0, rate
    assert c.observed_sec(now) <= 15.05


def test_창을_지난_사건은_빠진다():
    c = BlinkCounter(window_sec=10.0)
    now = feed(c, seconds=10.0, period_sec=2.0)
    n_before = c.count(now)
    assert n_before >= 3
    now2 = feed(c, seconds=12.0, period_sec=100.0, t0=now)   # 12초 동안 깜빡임 없음
    assert c.count(now2) == 0
    assert c.rate(now2) == 0.0
    assert c.observed_sec(now2) == 10.0                       # 창을 넘지 않습니다


def test_너무_짧거나_긴_감음은_세지_않는다():
    c = BlinkCounter()
    feed(c, seconds=12.0, period_sec=2.0, closed_sec=0.02)    # 20ms — 노이즈
    assert c.count(12.0) == 0
    c2 = BlinkCounter()
    feed(c2, seconds=12.0, period_sec=4.0, closed_sec=1.0)    # 1초 — 감고 있는 것
    assert c2.count(12.0) == 0


def test_깜빡임_지속시간을_기록한다():
    c = BlinkCounter()
    assert c.last_duration_ms is None
    feed(c, seconds=12.0, period_sec=3.0, closed_sec=0.2)
    assert c.last_duration_ms is not None
    assert 150 <= c.last_duration_ms <= 300


def test_히스테리시스_임계가_뒤집혀_있지_않다():
    assert EAR_CLOSED < EAR_OPEN


def test_eye_ear_는_거리에_영향받지_않는다():
    """비율이므로 얼굴이 2배로 커져도 같은 값이어야 합니다."""
    idx = (0, 1, 2, 3, 4, 5)
    small = {0: (0, 0), 1: (2, -1), 2: (6, -1), 3: (8, 0), 4: (6, 1), 5: (2, 1)}
    big = {k: (x * 2, y * 2) for k, (x, y) in small.items()}
    assert abs(eye_ear(small, idx) - eye_ear(big, idx)) < 1e-9
