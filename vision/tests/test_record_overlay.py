"""
vision/eval/record.py 의 그리기 부분 테스트.

카메라 없이 numpy 배열로 확인합니다. 이 부분이 루프 안에 있을 때
바깥 변수(h, w)에 기대다가 NameError 로 죽었고, 첫 신호가 뜨는 5초에
녹화가 통째로 끝났습니다. 사람이 화면을 보고 있어야만 발견되는 종류의
고장이라 테스트로 묶어 둡니다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))
sys.path.insert(0, str(ROOT / "vision" / "eval"))

pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from record import draw_overlay  # noqa: E402


def blank(h=480, w=640):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_신호일_때_창_전체가_초록이_된다():
    out = draw_overlay(blank(), cue=True, info="")
    corner = out[5, 5]
    assert tuple(int(v) for v in corner) == (0, 200, 0)   # BGR


def test_신호가_아니면_영상이_남는다():
    frame = blank()
    frame[:] = (30, 30, 30)
    out = draw_overlay(frame, cue=False, info="10.0s / 90s   EAR 0.300")
    assert tuple(int(v) for v in out[5, 5]) == (30, 30, 30)


def test_프레임_크기가_달라도_동작한다():
    """바깥 변수에 기대면 크기가 바뀌는 순간 깨집니다."""
    for h, w in [(240, 320), (720, 1280), (1080, 1920)]:
        out = draw_overlay(blank(h, w), cue=True, info="")
        assert out.shape == (h, w, 3)
