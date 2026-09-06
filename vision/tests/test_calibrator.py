"""vision/calibrator.py 테스트. 카메라 없이 돌아갑니다."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
import calibrator as C  # noqa: E402
from calibrator import Calibrator  # noqa: E402


def make(tmp_path, cm=60.0):
    return Calibrator(baseline_path=tmp_path / "baseline.json", calib_distance_cm=cm)


def test_샘플이_모이면_baseline_이_생긴다(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"face_width_px": 200.0, "blink_rate": 15.0})
    assert c.is_done()
    assert c.baseline_distance_cm() == 60.0
    assert (tmp_path / "baseline.json").exists()


def test_핀홀_환산():
    """face_width_px x distance = 상수."""
    c = Calibrator(baseline_path=Path("/nonexistent/baseline.json"), calib_distance_cm=60.0)
    c._baseline = {"face_width_px": 200.0, "calib_distance_cm": 60.0}
    c._done = True
    assert c.distance_cm(200.0) == 60.0
    assert c.distance_cm(400.0) == 30.0       # 얼굴이 2배로 커지면 거리는 절반
    assert c.distance_cm(0.0) is None


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
    c.add_sample({"face_width_px": 200.0, "blink_rate": 15.0})
    assert c.should_retry() is False


def test_저장된_기준거리가_다르면_경고한다(tmp_path, capsys):
    (tmp_path / "baseline.json").write_text(json.dumps({
        "face_width_px": 200.0, "blink_rate": 15.0, "calib_distance_cm": 60.0}))
    make(tmp_path, cm=45.0)                   # 자로 45cm 를 재서 넘겼는데
    out = capsys.readouterr().out
    assert "45" in out and "recalibrate" in out   # 조용히 무시되면 안 됩니다


def test_캘리브_깜빡임이_0이면_baseline_으로_쓰지_않는다(tmp_path, monkeypatch):
    """3초 창에서 잰 blink_rate 는 0 에 가깝습니다. 그것을 평소값이라 부르면 안 됩니다."""
    monkeypatch.setattr(C, "CALIB_DURATION", 0.0)
    c = make(tmp_path)
    c.start()
    c.add_sample({"face_width_px": 200.0, "blink_rate": 0.0})
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
