"""vision/distance.py 테스트. 카메라 없이 돌아갑니다."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
from distance import DistanceEstimator  # noqa: E402
from geometry import IRIS_DIAMETER_CM, LEFT_IRIS, RIGHT_IRIS, focal_px_from_iris  # noqa: E402
from test_iris import pts_with_iris  # noqa: E402


def test_화각에서_초점거리를_얻는다(monkeypatch):
    """자가 필요 없습니다 — 카메라 스펙만 있으면 됩니다."""
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)
    est = DistanceEstimator(image_width_px=1920, fov_deg=55.0)
    assert est.focal_source == "fov"
    assert 1800 < est.focal_px < 1900


def test_자로_잰_값이_화각보다_우선(monkeypatch):
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)
    est = DistanceEstimator(image_width_px=1920, focal_px=2000, fov_deg=55.0)
    assert est.focal_source == "measured"
    assert est.focal_px == 2000


def test_홍채로_거리를_구한다(monkeypatch):
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)
    f = focal_px_from_iris(40.0, 60.0)                  # 60cm 에서 홍채 40px
    est = DistanceEstimator(image_width_px=1920, focal_px=f)
    cm, src = est.distance_cm(pts_with_iris(40.0))
    assert src == "iris"
    assert abs(cm - 60.0) < 0.2
    cm2, _ = est.distance_cm(pts_with_iris(80.0))        # 홍채가 2배 → 거리 절반
    assert abs(cm2 - 30.0) < 0.2


def test_사람이_앉을_수_없는_거리는_버린다(monkeypatch):
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)
    f = focal_px_from_iris(40.0, 60.0)
    est = DistanceEstimator(image_width_px=1920, focal_px=f)
    cm, src = est.distance_cm(pts_with_iris(2.0))        # 홍채 2px → 1200cm
    assert cm is None and src is None


def test_홍채가_없으면_캘리브레이터로_버틴다(monkeypatch):
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)

    class FakeCalib:
        def distance_cm(self, width_px):
            return 55.5

    pts = {234: (100.0, 300.0), 454: (500.0, 300.0)}     # 홍채 랜드마크 없음
    est = DistanceEstimator(image_width_px=1920, fov_deg=55.0, calibrator=FakeCalib())
    cm, src = est.distance_cm(pts)
    assert (cm, src) == (55.5, "calib")


def test_아무것도_없으면_None(monkeypatch):
    monkeypatch.delenv("VISION_FOCAL_PX", raising=False)
    monkeypatch.delenv("VISION_CAMERA_FOV_DEG", raising=False)
    est = DistanceEstimator(image_width_px=0, fov_deg=0)
    assert est.distance_cm(pts_with_iris(40.0)) == (None, None)
