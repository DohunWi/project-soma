"""
vision/blink/ear.py 테스트. 카메라 없이 EAR 시계열을 합성해서 돌립니다.

    pytest vision/
"""
import sys
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "blink"))
from ear import (  # noqa: E402
    BlinkCounter,
    EAR_CLOSED,
    EAR_OPEN,
    LEFT_EYE,
    MAX_CLOSED_MS,
    MIN_OBSERVED_SEC,
    PHASE_CLOSED_CANDIDATE,
    PHASE_OPEN,
    PHASE_WAIT_FOR_RECOVERY,
    RECOVERY_OPEN_FRAMES,
    RECOVERY_OPEN_MS,
    RIGHT_EYE,
    emit_blink_diagnostic,
    eye_ear,
    face_ear,
    face_ear_values,
)

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


def test_60에서_500ms_사이의_감음은_정상_blink다():
    for duration_sec in (0.06, 0.2, 0.5):
        counter = BlinkCounter()

        assert counter.update(CLOSED_EAR, 0.0) is False
        assert counter.phase == PHASE_CLOSED_CANDIDATE
        assert counter.update(OPEN_EAR, duration_sec) is True

        assert counter.phase == PHASE_OPEN
        assert counter.count(duration_sec) == 1
        assert counter.last_duration_ms == duration_sec * 1000


def test_60ms보다_짧은_감음은_noise다():
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 10.0)

    assert counter.update(OPEN_EAR, 10.059) is False

    assert counter.phase == PHASE_OPEN
    assert counter.count(10.059) == 0
    assert counter.last_transition_reason == "noise"


def test_500ms를_넘긴_candidate는_event없이_recovery를_기다린다():
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 10.0)

    assert counter.update(CLOSED_EAR, 10.501) is False

    assert counter.phase == PHASE_WAIT_FOR_RECOVERY
    assert counter.count(10.501) == 0
    assert counter.last_transition_reason == "timeout"


def test_overlong_이후_stable_open에서만_recovery한다():
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 10.0)
    counter.update(CLOSED_EAR, 10.6)

    assert counter.update(OPEN_EAR, 10.7) is False
    assert counter.phase == PHASE_WAIT_FOR_RECOVERY
    assert counter.update(OPEN_EAR, 10.75) is False
    assert counter.phase == PHASE_WAIT_FOR_RECOVERY
    assert counter.update(OPEN_EAR, 10.81) is False

    assert counter.phase == PHASE_OPEN
    assert counter.last_transition_reason == "recovery"
    assert counter.count(10.81) == 0


def test_recovery중_ear가_낮아지면_stable_open_evidence를_다시_모은다():
    counter = BlinkCounter(recovery_open_ms=RECOVERY_OPEN_MS)
    counter.update(CLOSED_EAR, 10.0)
    counter.update(CLOSED_EAR, 10.6)
    counter.update(OPEN_EAR, 10.7)
    counter.update(CLOSED_EAR, 10.76)

    counter.update(OPEN_EAR, 10.9)
    counter.update(OPEN_EAR, 10.99)
    assert counter.phase == PHASE_WAIT_FOR_RECOVERY
    counter.update(OPEN_EAR, 11.01)

    assert counter.phase == PHASE_OPEN
    assert counter.count(11.01) == 0


def test_face_loss는_진행중_candidate를_invalidate한다():
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 10.0)

    assert counter.on_face_lost() is True

    assert counter.phase == PHASE_OPEN
    assert counter.candidate_duration_ms(10.2) is None
    assert counter.last_transition_reason == "face_loss"


def test_face_recovery가_이전_candidate를_이어받지_않는다():
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 10.0)
    counter.on_face_lost()

    assert counter.update(OPEN_EAR, 10.2) is False
    assert counter.count(10.2) == 0

    counter.update(CLOSED_EAR, 10.3)
    assert counter.update(OPEN_EAR, 10.5) is True
    assert counter.count(10.5) == 1


def test_threshold를_생성자에서_주입할_수_있다():
    counter = BlinkCounter(closed_threshold=0.15, open_threshold=0.20)

    counter.update(0.18, 10.0)
    assert counter.phase == PHASE_OPEN
    counter.update(0.14, 10.1)
    assert counter.phase == PHASE_CLOSED_CANDIDATE
    assert counter.update(0.21, 10.3) is True


def test_open_threshold는_closed_threshold보다_커야_한다():
    for open_threshold in (0.20, 0.19, float("nan")):
        try:
            BlinkCounter(closed_threshold=0.20, open_threshold=open_threshold)
        except ValueError as error:
            assert "open_threshold" in str(error)
        else:
            raise AssertionError("invalid threshold ordering was accepted")


def test_default_threshold는_기존_0_21과_0_25를_유지한다():
    counter = BlinkCounter()

    assert counter.closed_threshold == EAR_CLOSED == 0.21
    assert counter.open_threshold == EAR_OPEN == 0.25
    assert MAX_CLOSED_MS == 500
    assert counter.recovery_open_ms == RECOVERY_OPEN_MS == 100
    assert counter.recovery_open_frames == RECOVERY_OPEN_FRAMES == 3


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


def eye_points(left_height=2.0, right_height=1.0):
    pts = {}
    for indices, height in ((LEFT_EYE, left_height), (RIGHT_EYE, right_height)):
        values = (
            (0.0, 0.0),
            (2.0, -height),
            (6.0, -height),
            (8.0, 0.0),
            (6.0, height),
            (2.0, height),
        )
        pts.update(dict(zip(indices, values)))
    return pts


def diagnostic_values(counter, *, now, left, right, combined, before, blinked):
    return {
        "now": now,
        "left_ear": left,
        "right_ear": right,
        "combined_ear": combined,
        "phase_before": before,
        "counter": counter,
        "blinked": blinked,
    }


def test_좌우_평균값이_기존_face_ear와_같다():
    pts = eye_points()

    left, right, combined = face_ear_values(pts)

    assert combined == (left + right) / 2.0
    assert combined == face_ear(pts)


def test_debug_off는_아무것도_출력하지_않는다():
    stream = StringIO()
    counter = BlinkCounter()
    before = counter.phase
    blinked = counter.update(OPEN_EAR, 1000.0)

    result = emit_blink_diagnostic(
        False,
        stream=stream,
        **diagnostic_values(
            counter,
            now=1000.0,
            left=OPEN_EAR,
            right=OPEN_EAR,
            combined=OPEN_EAR,
            before=before,
            blinked=blinked,
        ),
    )

    assert result is None
    assert stream.getvalue() == ""


def test_debug_on은_raw_metric과_candidate_transition을_stderr에_출력한다(capsys):
    counter = BlinkCounter()
    before = counter.phase
    blinked = counter.update(CLOSED_EAR, 1000.0)

    emit_blink_diagnostic(
        True,
        **diagnostic_values(
            counter,
            now=1000.0,
            left=0.1000,
            right=0.1200,
            combined=0.1100,
            before=before,
            blinked=blinked,
        ),
    )
    captured = capsys.readouterr()
    output = captured.err

    assert captured.out == ""
    assert "timestamp=1970-01-01T00:16:40.000Z" in output
    assert "left_ear=0.1000 right_ear=0.1200 combined_ear=0.1100" in output
    assert "threshold_closed=<0.210 threshold_open=>0.250" in output
    assert "eye=CLOSED state=CLOSED_CANDIDATE" in output
    assert "transition=OPEN->CLOSED_CANDIDATE" in output
    assert "event=false" in output


def test_debug_on은_blink_event와_duration을_출력한다():
    stream = StringIO()
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 1000.0)
    before = counter.phase
    blinked = counter.update(OPEN_EAR, 1000.2)

    emit_blink_diagnostic(
        True,
        stream=stream,
        **diagnostic_values(
            counter,
            now=1000.2,
            left=0.3000,
            right=0.3000,
            combined=0.3000,
            before=before,
            blinked=blinked,
        ),
    )
    output = stream.getvalue()

    assert blinked is True
    assert "eye=OPEN state=OPEN" in output
    assert "transition=CLOSED_CANDIDATE->OPEN" in output
    assert "reason=blink" in output
    assert "event=true duration_ms=200.0" in output


def test_debug는_timeout과_stable_recovery_transition을_구분한다():
    stream = StringIO()
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 1000.0)
    before = counter.phase
    blinked = counter.update(CLOSED_EAR, 1000.6)
    emit_blink_diagnostic(
        True,
        stream=stream,
        **diagnostic_values(
            counter,
            now=1000.6,
            left=CLOSED_EAR,
            right=CLOSED_EAR,
            combined=CLOSED_EAR,
            before=before,
            blinked=blinked,
        ),
    )
    assert "transition=CLOSED_CANDIDATE->WAIT_FOR_RECOVERY" in stream.getvalue()
    assert "reason=timeout" in stream.getvalue()

    counter.update(OPEN_EAR, 1000.7)
    counter.update(OPEN_EAR, 1000.75)
    before = counter.phase
    counter.update(OPEN_EAR, 1000.81)
    emit_blink_diagnostic(
        True,
        stream=stream,
        **diagnostic_values(
            counter,
            now=1000.81,
            left=OPEN_EAR,
            right=OPEN_EAR,
            combined=OPEN_EAR,
            before=before,
            blinked=False,
        ),
    )
    output = stream.getvalue()
    assert "transition=WAIT_FOR_RECOVERY->OPEN" in output
    assert "reason=recovery" in output


def test_debug는_face_loss_invalidation을_구분한다():
    stream = StringIO()
    counter = BlinkCounter()
    counter.update(CLOSED_EAR, 1000.0)
    before = counter.phase

    assert counter.on_face_lost() is True
    emit_blink_diagnostic(
        True,
        stream=stream,
        **diagnostic_values(
            counter,
            now=1000.1,
            left=None,
            right=None,
            combined=None,
            before=before,
            blinked=False,
        ),
    )

    output = stream.getvalue()
    assert "eye=NO_FACE state=OPEN" in output
    assert "transition=CLOSED_CANDIDATE->OPEN" in output
    assert "reason=face_loss" in output
