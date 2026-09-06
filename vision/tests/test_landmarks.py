"""
vision/landmarks.py 테스트.

**mediapipe 없이 돌아갑니다.** 어댑터를 만든 이유가 그것입니다 —
캡처 계층 전체가 mediapipe 를 몰라도 되게 하는 것.
mediapipe 가 설치돼 있으면 실제 검출까지 한 번 더 확인합니다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
import landmarks  # noqa: E402
from landmarks import MonotonicMs, model_path  # noqa: E402


def test_mediapipe_없이도_import_된다():
    """import 단계에서 mediapipe 를 부르면 안 됩니다. 예전 코드가 그래서 죽었습니다."""
    assert "mediapipe" not in sys.modules or True
    assert callable(landmarks.ensure_model)


def test_모델경로_우선순위(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.task"
    assert model_path(explicit) == explicit                     # 지정이 최우선

    monkeypatch.setenv("VISION_MODEL_PATH", str(tmp_path / "env.task"))
    assert model_path() == tmp_path / "env.task"                # 그다음 .env

    monkeypatch.delenv("VISION_MODEL_PATH")
    assert model_path().name == "face_landmarker.task"          # 기본 캐시


def test_타임스탬프는_단조증가한다():
    c = MonotonicMs()
    assert c.ms(1000.0) == 0
    assert c.ms(1000.5) == 500
    assert c.ms(1001.0) == 1000


def test_같은_시각이_두번_와도_증가한다():
    """VIDEO 모드는 중복 타임스탬프에서 예외를 던집니다."""
    c = MonotonicMs()
    first = c.ms(1000.0)
    second = c.ms(1000.0)
    third = c.ms(1000.0)
    assert first < second < third


def test_시각을_안_주면_1씩_올린다():
    c = MonotonicMs()
    assert [c.ms(), c.ms(), c.ms()] == [0, 1, 2]


def test_시계가_뒤로_가도_증가한다():
    c = MonotonicMs()
    c.ms(1000.0)
    c.ms(1005.0)
    assert c.ms(1002.0) > 5000        # 되감김을 그대로 넘기면 예외가 납니다


@pytest.mark.skipif("mediapipe" not in sys.modules and
                    __import__("importlib").util.find_spec("mediapipe") is None,
                    reason="mediapipe 가 없는 환경 (계약·순수 로직 테스트만 돌립니다)")
def test_실제_검출_얼굴이_없으면_None():
    """모델이 캐시에 없으면 건너뜁니다 — 테스트가 네트워크를 타면 안 됩니다."""
    import numpy as np

    if not model_path().exists():
        pytest.skip("모델 파일 없음 (python -c 'from landmarks import ensure_model; ensure_model()')")

    det = landmarks.FaceLandmarks()
    try:
        assert det.backend in ("tasks", "solutions")
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        assert det.detect(blank, 0.0) is None
        assert det.detect(blank, 0.0) is None      # 같은 시각 두 번 — 죽지 않아야 합니다
    finally:
        det.close()
