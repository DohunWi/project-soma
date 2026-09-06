"""vision/camera.py 테스트. 카메라·OpenCV 없이 돌아갑니다."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
from camera import default_index, parse_camera_names  # noqa: E402

SAMPLE = """Camera:

    FaceTime HD Camera:

      Model ID: FaceTime HD Camera
      Unique ID: FDF90FEB-59E5-4FCF-AABD-DA03C4E19BFB

    장봉팔 Camera:

      Model ID: iPhone17,1
      Unique ID: 14FD7913-7C9C-4209-A647-370900000001
"""


def test_지정값이_최우선(monkeypatch):
    monkeypatch.setenv("WEBCAM_INDEX", "1")
    assert default_index(3) == 3


def test_지정이_없으면_env를_본다(monkeypatch):
    """녹화 도구가 .env 를 무시하고 0번(iPhone)을 열던 것을 막습니다."""
    monkeypatch.setenv("WEBCAM_INDEX", "1")
    assert default_index() == 1
    assert default_index(None) == 1


def test_env도_없으면_0(monkeypatch):
    monkeypatch.delenv("WEBCAM_INDEX", raising=False)
    assert default_index() == 0


def test_장치이름을_읽는다():
    assert parse_camera_names(SAMPLE) == ["FaceTime HD Camera", "장봉팔 Camera"]


def test_이름이_없으면_빈_목록():
    assert parse_camera_names("") == []
    assert parse_camera_names("Camera:\n") == []
