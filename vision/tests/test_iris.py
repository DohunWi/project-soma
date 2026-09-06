"""vision/geometry.py 의 홍채 기반 거리 테스트. 카메라 없이 돌아갑니다."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
from geometry import (IRIS_DIAMETER_CM, distance_cm_from_iris,  # noqa: E402
                      face_width_cm_from_iris, focal_px_from_iris, iris_px,
                      LEFT_IRIS, RIGHT_IRIS)


def pts_with_iris(iris_px_size=30.0, cx=500.0):
    """양쪽 홍채가 같은 크기인 가짜 랜드마크."""
    p = {}
    for idx, base in ((LEFT_IRIS, cx), (RIGHT_IRIS, cx - 200)):
        c, r, up, left, dn = idx
        p[c] = (base, 300.0)
        p[r] = (base + iris_px_size / 2, 300.0)
        p[left] = (base - iris_px_size / 2, 300.0)
        p[up] = (base, 300.0 - iris_px_size / 2)
        p[dn] = (base, 300.0 + iris_px_size / 2)
    return p


def test_홍채_지름은_좌우_평균():
    assert abs(iris_px(pts_with_iris(30.0)) - 30.0) < 1e-9


def test_홍채_랜드마크가_없으면_None():
    assert iris_px({0: (1.0, 1.0)}) is None


def test_자로_한번_재면_초점거리가_나온다():
    """사람마다가 아니라 카메라마다 한 번입니다."""
    f = focal_px_from_iris(30.0, 60.0)
    assert abs(f - 30.0 * 60.0 / IRIS_DIAMETER_CM) < 1e-9
    assert abs(distance_cm_from_iris(30.0, f) - 60.0) < 1e-9


def test_거리는_홍채_크기에_반비례():
    f = focal_px_from_iris(30.0, 60.0)
    assert abs(distance_cm_from_iris(60.0, f) - 30.0) < 1e-9
    assert abs(distance_cm_from_iris(15.0, f) - 120.0) < 1e-9


def test_얼굴폭은_자_없이도_구해진다():
    """홍채와 얼굴이 같은 거리·같은 렌즈라 비율만 남습니다."""
    w = face_width_cm_from_iris(face_width_px_value=360.0, iris_diameter_px_value=30.0)
    assert abs(w - IRIS_DIAMETER_CM * 12.0) < 1e-9      # 14.04cm

    # 거리가 2배가 되면 둘 다 절반이 되고 결과는 같아야 합니다
    w2 = face_width_cm_from_iris(180.0, 15.0)
    assert abs(w - w2) < 1e-9


def test_홍채가_0이면_None():
    assert distance_cm_from_iris(0.0, 1900.0) is None
    assert face_width_cm_from_iris(300.0, 0.0) is None
