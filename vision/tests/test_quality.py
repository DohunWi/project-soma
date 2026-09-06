"""vision/quality.py 테스트. 순수 클래스라 카메라가 필요 없습니다."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
from quality import FrameQuality  # noqa: E402


def test_표본이_없으면_None():
    q = FrameQuality()
    assert q.detect_rate(0.0) is None
    assert q.yaw_dropped_rate(0.0) is None


def test_검출률():
    q = FrameQuality(window_sec=10.0)
    for i in range(10):
        q.update(i * 0.1, detected=(i < 8), frontal=True if i < 8 else None)
    assert q.detect_rate(1.0) == 0.8


def test_고개돌림_폐기율의_분모는_검출프레임():
    """자리를 비우면 분모가 커져 폐기율이 0 으로 희석되면 안 됩니다."""
    q = FrameQuality(window_sec=10.0)
    for i in range(5):                       # 검출 5프레임 중 4개가 고개 돌림
        q.update(i * 0.1, detected=True, frontal=(i == 0))
    for i in range(5, 20):                   # 자리 비움 15프레임
        q.update(i * 0.1, detected=False, frontal=None)
    assert q.yaw_dropped_rate(2.0) == 0.8
    assert q.detect_rate(2.0) == 0.25


def test_창을_지난_프레임은_빠진다():
    q = FrameQuality(window_sec=1.0)
    for i in range(10):
        q.update(i * 0.1, detected=False, frontal=None)
    for i in range(10):
        q.update(2.0 + i * 0.1, detected=True, frontal=True)
    assert q.frames(2.9) == 10
    assert q.detect_rate(2.9) == 1.0
