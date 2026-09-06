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
