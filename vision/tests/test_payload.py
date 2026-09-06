"""
vision/payload.py 테스트 — **계약 파일로 직접 검증합니다.**

하드웨어 없이 돌아갑니다.  pytest vision/

이 테스트가 있는 이유: 이전에는 얼굴이 안 잡히거나 고개를 돌리면
face_distance_cm 에 null 이 들어갔고, 서버가 스키마 위반으로 payload 를
통째로 버렸습니다. 깜빡임 값까지 같이 사라졌는데 아무도 몰랐습니다.
계약을 코드 안에 베껴 적지 않고 스키마 파일을 읽어서 검증합니다.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
from payload import vision_payload  # noqa: E402

jsonschema = pytest.importorskip("jsonschema")
SCHEMA = json.loads((ROOT / "docs/contracts/sensor_data.schema.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def errors(ev):
    return [f"{list(e.path)}: {e.message}" for e in VALIDATOR.iter_errors(ev)]


def nulls(obj, path=""):
    """payload 어디에도 None 이 남아 있으면 안 됩니다."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += nulls(v, f"{path}.{k}")
    elif obj is None:
        out.append(path)
    return out


def test_정상_payload가_계약을_통과한다():
    ev = vision_payload(t=1725345600.15, user_name="guest", face_detected=True,
                        blink=True, blink_rate=8.4, blink_count=8, window_sec=57.1,
                        blink_duration_ms=142.0, face_distance_cm=48.2,
                        face_distance_baseline_cm=60.0, blink_rate_baseline=15.2,
                        detect_rate=0.97, yaw_dropped_rate=0.08, calibrating=False)
    assert errors(ev) == []
    assert ev["source"] == "vision" and ev["v"] == 1


def test_얼굴이_없으면_거리키를_생략한다():
    ev = vision_payload(t=1000.0, user_name="g", face_detected=False,
                        blink_rate=None, blink_count=3, window_sec=12.0,
                        face_distance_cm=None, detect_rate=0.0)
    assert errors(ev) == []
    assert "face_distance_cm" not in ev["vision"]
    assert "blink_rate" not in ev["vision"]
    assert ev["vision"]["blink_count"] == 3          # 깜빡임 값은 살아남습니다


def test_어디에도_null을_넣지_않는다():
    ev = vision_payload(t=1000.0, user_name="g", face_detected=False)
    assert nulls(ev) == []
    assert errors(ev) == []


def test_0이하_거리는_키를_뺀다():
    """이전 구현은 0.0 을 보냈고 fusion 이 dist <= 0 을 특수값으로 해석해야 했습니다."""
    ev = vision_payload(t=1000.0, user_name="g", face_detected=True, face_distance_cm=0.0)
    assert "face_distance_cm" not in ev["vision"]
    assert errors(ev) == []


def test_비율은_0에서_1로_잘린다():
    ev = vision_payload(t=1000.0, user_name="g", face_detected=True,
                        detect_rate=1.7, yaw_dropped_rate=-0.2)
    assert ev["vision"]["detect_rate"] == 1.0
    assert ev["vision"]["yaw_dropped_rate"] == 0.0
    assert errors(ev) == []


def test_고개_돌림_상태도_계약을_통과한다():
    """검출은 됐지만 정면이 아니라 거리만 없는 경우."""
    ev = vision_payload(t=1000.0, user_name="g", face_detected=True,
                        blink_rate=12.0, blink_count=12, window_sec=60.0,
                        face_distance_cm=None, detect_rate=1.0, yaw_dropped_rate=0.9,
                        calibrating=False)
    assert errors(ev) == []
    assert ev["vision"]["yaw_dropped_rate"] == 0.9   # UI 가 이유를 설명할 수 있습니다


def test_캘리브레이션_중임을_알린다():
    ev = vision_payload(t=1000.0, user_name="g", face_detected=True, calibrating=True)
    assert ev["vision"]["calibrating"] is True
    assert errors(ev) == []
