"""
vision/eval/sweep.py 채점 테스트.

신호 사이의 자연 깜빡임을 오검출로 세면 정밀도가 왜곡되고, 그 값으로
임계를 고르게 됩니다. 그 구분을 테스트로 고정합니다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision" / "eval"))
from sweep import detect, score  # noqa: E402


def test_신호에_맞춘_검출은_재현율로_잡힌다():
    cues = [10.0, 14.0, 18.0]
    dets = [10.1, 14.05, 17.9]
    r = score(cues, dets, holds=[], span=30.0)
    assert r["recall"] == 1.0
    assert r["fn"] == 0
    assert r["unlabeled"] == 0


def test_버티기_구간의_검출만_오검출이다():
    cues = [20.0]
    dets = [5.0, 7.0, 20.1]          # 앞 두 개는 눈 뜨고 버티는 중 → 진짜 오검출
    r = score(cues, dets, holds=[(0.0, 15.0)], span=30.0)
    assert r["fp"] == 2
    assert r["fp_per_min"] == 8.0    # 15초에 2회 → 분당 8
    assert r["recall"] == 1.0
    assert r["unlabeled"] == 0


def test_신호_사이의_자연_깜빡임은_오검출이_아니다():
    """이것을 FP 로 세면 검출기가 맞게 잡은 것을 틀렸다고 세는 것입니다."""
    cues = [20.0, 24.0]
    dets = [20.1, 22.0, 24.1, 26.0]  # 22.0, 26.0 은 자연 깜빡임
    r = score(cues, dets, holds=[(0.0, 15.0)], span=30.0)
    assert r["fp"] == 0
    assert r["unlabeled"] == 2
    assert r["recall"] == 1.0


def test_라벨없는_검출은_분당으로_환산한다():
    cues = []
    dets = [20.0, 40.0, 60.0]
    r = score(cues, dets, holds=[(0.0, 15.0)], span=75.0)   # 자유 구간 60초
    assert r["unlabeled_per_min"] == 3.0


def test_버티기_구간이_없으면_오검출을_모른다():
    r = score([10.0], [10.1, 12.0], holds=[], span=30.0)
    assert r["fp_per_min"] is None       # 모르는 것을 0 이라고 말하지 않습니다


def test_놓친_신호는_재현율을_떨어뜨린다():
    r = score([10.0, 14.0, 18.0], [10.1], holds=[], span=30.0)
    assert r["tp"] == 1 and r["fn"] == 2
    assert abs(r["recall"] - 1 / 3) < 1e-9


def test_감은시간_게이트가_검출을_거른다():
    frames = [(0.0, 0.30), (0.05, 0.10), (0.08, 0.10), (0.12, 0.30)]   # 70ms 감음
    assert len(detect(frames, 0.21, 0.25, 60, 500)) == 1
    assert len(detect(frames, 0.21, 0.25, 100, 500)) == 0    # 100ms 미만은 버림
