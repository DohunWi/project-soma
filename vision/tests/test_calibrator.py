"""vision/calibrator.py 테스트. 카메라 없이 돌아갑니다."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
import calibrator as C  # noqa: E402
from calibrator import Calibrator  # noqa: E402


def make(tmp_path):
    return Calibrator(baseline_path=tmp_path / "baseline.json")


def test_샘플이_모이면_baseline_이_생긴다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"distance_cm": 62.0, "blink_rate": 15.0})
    assert c.is_done()
    assert c.baseline_distance_cm() == 62.0        # 잰 값. 가정한 값이 아닙니다
    assert (tmp_path / "baseline.json").exists()


def test_평소_거리는_샘플의_평균이다(tmp_path, monkeypatch):
    """스케일을 만들지 않습니다. 그 사람이 평소 어디에 앉는지만 기록합니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 10.0)
    c = make(tmp_path)
    c.start()
    for cm in (60.0, 64.0, 62.0):
        c.add_sample({"distance_cm": cm, "blink_rate": 12.0})
    c.tick(c._start + 11.0)
    assert abs(c.baseline_distance_cm() - 62.0) < 1e-9


def test_샘플이_없으면_실패하고_재시도를_허용한다(tmp_path, monkeypatch):
    """이전에는 실패가 최종 상태였고, 그 실행 내내 거리값이 None 이었습니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    monkeypatch.setattr(C, "CALIB_RETRY_SEC", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({})                        # 쓸 수 있는 값이 없는 프레임
    assert not c.is_done()
    assert not c.is_calibrating()
    assert c.attempts() == 1
    assert c.should_retry() is True          # 다시 시도할 수 있어야 합니다


def test_재시도에는_간격이_있다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    monkeypatch.setattr(C, "CALIB_RETRY_SEC", 999.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({})
    assert c.should_retry() is False          # 매 프레임 재시도하면 로그가 폭주합니다


def test_완료되면_재시도하지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"distance_cm": 60.0, "blink_rate": 15.0})
    assert c.should_retry() is False


def test_옛_baseline_은_버린다(tmp_path, capsys):
    """얼굴폭 기준의 옛 파일은 새 스케일과 의미가 다릅니다. 이어서 쓰면 안 됩니다."""
    (tmp_path / "baseline.json").write_text(json.dumps({
        "face_width_px": 449.1, "blink_rate": 0.0, "calib_distance_cm": 60.0}))
    c = make(tmp_path)
    assert not c.is_done()
    assert "재캘리브레이션" in capsys.readouterr().out


def test_캘리브_깜빡임이_0이면_baseline_으로_쓰지_않는다(tmp_path, monkeypatch):
    """3초 창에서 잰 blink_rate 는 0 에 가깝습니다. 그것을 평소값이라 부르면 안 됩니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"distance_cm": 60.0, "blink_rate": 0.0})
    assert c.is_done()
    assert c.baseline_blink_rate() is None


def test_얼굴이_안_보여도_창은_끝난다(tmp_path, monkeypatch):
    """add_sample 은 정면 프레임에서만 불립니다. 마감을 거기서만 판단하면
    얼굴이 없을 때 캘리브레이션이 영원히 끝나지 않습니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 3.0)
    c = make(tmp_path)
    c.start()
    assert c.is_calibrating()
    c.tick(c._start + 1.0)
    assert c.is_calibrating()          # 아직 창 안
    c.tick(c._start + 3.1)
    assert not c.is_calibrating()      # 창이 끝나면 실패로 마감
    assert not c.is_done()
    assert c.attempts() == 1


def test_다른_초점거리로_잰_평소값은_버린다(tmp_path, capsys, monkeypatch):
    """평소 거리는 초점거리에 딸린 값입니다. 카메라가 바뀌면 다른 자로 잰 숫자입니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = Calibrator(baseline_path=tmp_path / "baseline.json", focal_px=1844.0)
    c.start()
    c.add_sample({"distance_cm": 55.1, "blink_rate": 12.0})
    assert c.is_done()

    c2 = Calibrator(baseline_path=tmp_path / "baseline.json", focal_px=1484.0)
    assert not c2.is_done()                       # 이어 쓰지 않습니다
    assert "재캘리브레이션" in capsys.readouterr().out


def test_같은_초점거리면_이어_쓴다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = Calibrator(baseline_path=tmp_path / "baseline.json", focal_px=1484.0)
    c.start()
    c.add_sample({"distance_cm": 55.1, "blink_rate": 12.0})

    c2 = Calibrator(baseline_path=tmp_path / "baseline.json", focal_px=1484.0)
    assert c2.is_done()
    assert c2.baseline_distance_cm() == 55.1


def test_카메라를_연_뒤에_초점거리를_묶는다(tmp_path, monkeypatch, capsys):
    """초점거리는 카메라가 열린 뒤에 정해집니다. 생성 시점에는 모릅니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = Calibrator(baseline_path=tmp_path / "baseline.json", focal_px=1844.0)
    c.start()
    c.add_sample({"distance_cm": 55.1, "blink_rate": 12.0})

    c2 = Calibrator(baseline_path=tmp_path / "baseline.json")
    assert c2.is_done()                # 아직 모르므로 일단 로드
    c2.bind_focal(1484.0)
    assert not c2.is_done()            # 알고 나면 버립니다
    assert c2.should_retry() is True


# ── 평소 깜빡임 ──────────────────────────────────────────────────────────────

def test_평소_깜빡임은_구간의_중앙값(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 10.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 5)
    c = make(tmp_path)
    rates = [20.0, 22.0, 18.0, 25.0, 21.0, 19.0]
    for i, r in enumerate(rates):
        c.add_blink_sample(r, 1000.0 + i)
    assert c.baseline_blink_rate() is None          # 아직 구간이 안 끝났습니다
    c.add_blink_sample(21.0, 1011.0)
    assert c.baseline_blink_rate() == 21.0


def test_값이_없는_프레임은_평소값에_섞지_않는다(tmp_path, monkeypatch):
    """관측 10초 미만이면 rate 가 None 입니다. 그것을 0 으로 세면 안 됩니다."""
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 2)
    c = make(tmp_path)
    for i in range(5):
        c.add_blink_sample(None, 1000.0 + i)
    assert c.baseline_blink_rate() is None
    assert c.blink_baseline_progress() == 0.0


def test_표본이_모자라면_구간이_끝나도_확정하지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 100)
    c = make(tmp_path)
    for i in range(10):
        c.add_blink_sample(20.0, 1000.0 + i)
    assert c.baseline_blink_rate() is None


def test_한번_정해지면_다시_재지_않는다(tmp_path, monkeypatch):
    """피로로 깜빡임이 줄면 baseline 도 따라 내려가서는 안 됩니다."""
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 2)
    c = make(tmp_path)
    c.add_blink_sample(20.0, 1000.0)
    c.add_blink_sample(20.0, 1002.0)
    assert c.baseline_blink_rate() == 20.0
    for i in range(50):
        c.add_blink_sample(4.0, 1010.0 + i)      # 피로로 급감
    assert c.baseline_blink_rate() == 20.0
    assert c.needs_blink_baseline() is False


def test_평소_깜빡임은_다음_세션에도_남는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 2)
    c = make(tmp_path)
    c.add_blink_sample(17.0, 1000.0)
    c.add_blink_sample(17.0, 1002.0)

    c2 = make(tmp_path)
    assert c2.baseline_blink_rate() == 17.0


def test_거리_baseline_이_없어도_평소_깜빡임은_살린다(tmp_path, capsys):
    """5분을 들여 잰 값을 거리 캘리브레이션이 없다고 같이 버리면 안 됩니다."""
    (tmp_path / "baseline.json").write_text(json.dumps({"blink_rate": 18.5}))
    c = make(tmp_path)
    assert c.baseline_blink_rate() == 18.5
    assert not c.is_done()                        # 거리는 여전히 필요합니다


# ── 평소 거리 다듬기 ─────────────────────────────────────────────────────────

def ready(tmp_path, monkeypatch, cm=35.4):
    """3초 캘리브레이션이 끝난 상태를 만듭니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"distance_cm": cm})
    assert c.is_done()
    return c


def test_3초값은_임시로_표시된다(tmp_path, monkeypatch):
    c = ready(tmp_path, monkeypatch)
    assert c.distance_baseline_is_provisional() is True
    assert c.baseline_distance_cm() == 35.4


def test_5분_중앙값으로_덮어쓴다(tmp_path, monkeypatch):
    """3초 캘리브레이션은 하필 그때의 자세를 평소라고 부릅니다."""
    monkeypatch.setattr(C, "DIST_BASELINE_SEC", 10.0)
    monkeypatch.setattr(C, "DIST_MIN_SAMPLES", 5)
    c = ready(tmp_path, monkeypatch, cm=35.4)          # 몸을 기울인 순간
    for i, cm in enumerate([42.0, 43.0, 42.5, 41.0, 44.0, 42.6]):
        c.add_distance_sample(cm, 1000.0 + i)
    assert c.baseline_distance_cm() == 35.4            # 아직 구간 중
    c.add_distance_sample(42.6, 1011.0)
    assert c.baseline_distance_cm() == 42.6
    assert c.distance_baseline_is_provisional() is False


def test_확정되면_다시_바꾸지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DIST_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "DIST_MIN_SAMPLES", 2)
    c = ready(tmp_path, monkeypatch)
    c.add_distance_sample(42.0, 1000.0)
    c.add_distance_sample(42.0, 1002.0)
    assert c.baseline_distance_cm() == 42.0
    for i in range(50):
        c.add_distance_sample(30.0, 1010.0 + i)        # 점점 화면에 붙어도
    assert c.baseline_distance_cm() == 42.0            # 평소값은 그대로


def test_값이_없으면_섞지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DIST_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "DIST_MIN_SAMPLES", 2)
    c = ready(tmp_path, monkeypatch)
    for i in range(5):
        c.add_distance_sample(None, 1000.0 + i)
    assert c.distance_baseline_is_provisional() is True


def test_확정값은_다음_세션에도_남는다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DIST_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "DIST_MIN_SAMPLES", 2)
    c = ready(tmp_path, monkeypatch)
    c.add_distance_sample(42.0, 1000.0)
    c.add_distance_sample(42.0, 1002.0)

    c2 = make(tmp_path)
    assert c2.baseline_distance_cm() == 42.0
    assert c2.distance_baseline_is_provisional() is False


def test_거리_재측정이_평소_깜빡임을_지우지_않는다(tmp_path, monkeypatch):
    """깜빡임 평소값은 5분짜리입니다. 거리 3초 재측정에 딸려 버려지면 안 됩니다."""
    monkeypatch.setattr(C, "BLINK_BASELINE_SEC", 1.0)
    monkeypatch.setattr(C, "BLINK_MIN_SAMPLES", 2)
    c = ready(tmp_path, monkeypatch)
    c.add_blink_sample(21.7, 1000.0)
    c.add_blink_sample(21.7, 1002.0)
    assert c.baseline_blink_rate() == 21.7

    c.recalibrate()                                    # 거리만 다시 잽니다
    c.add_sample({"distance_cm": 50.0})
    assert c.baseline_distance_cm() == 50.0
    assert c.baseline_blink_rate() == 21.7
