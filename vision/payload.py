"""
vision/payload.py
─────────────────
계약 payload(`sensor_data`, `source="vision"`) 를 만드는 **순수 함수**.

카메라·소켓·mediapipe 를 import 하지 않습니다. 그래서 하드웨어 없이
pytest 로 계약 준수를 검증할 수 있습니다 — vision/tests/test_payload.py 가
실제 스키마 파일로 이 함수의 출력을 검증합니다.

**값이 없으면 키를 생략합니다. null 을 넣지 않습니다.**
`face_distance_cm: null` 을 보내면 스키마 위반이라 서버가 payload 를 통째로
버립니다. 거리 하나 때문에 같이 실려 있던 깜빡임 값까지 사라집니다.
(docs/contracts/README.md 규칙 5)

    ev = vision_payload(t=now, user_name="guest", face_detected=True,
                        blink_rate=8.4, blink_count=8, window_sec=57.1)
"""
V = 1


def _put(d: dict, key: str, value) -> None:
    """None 이면 넣지 않습니다. 이 함수가 이 파일의 존재 이유입니다."""
    if value is not None:
        d[key] = value


def _clamp01(v):
    if v is None:
        return None
    return round(min(max(float(v), 0.0), 1.0), 3)


def vision_payload(*, t: float, user_name: str, face_detected: bool,
                   blink: bool = False,
                   blink_rate=None, blink_count=None, window_sec=None,
                   blink_duration_ms=None,
                   face_distance_cm=None, face_distance_baseline_cm=None,
                   blink_rate_baseline=None,
                   detect_rate=None, yaw_dropped_rate=None,
                   calibrating=None) -> dict:
    """docs/contracts/sensor_data.schema.json 을 따르는 dict 를 만듭니다."""
    v = {"blink": bool(blink), "face_detected": bool(face_detected)}

    _put(v, "blink_rate", blink_rate)
    _put(v, "blink_count", None if blink_count is None else int(blink_count))
    _put(v, "window_sec", None if window_sec is None else round(float(window_sec), 1))
    _put(v, "blink_duration_ms", blink_duration_ms)

    # 거리는 양수일 때만 의미가 있습니다. 0 이나 음수는 "없음" 이므로 키를 뺍니다 —
    # 이전 구현은 0.0 을 보냈고, fusion 이 dist <= 0 을 특수값으로 해석해야 했습니다.
    if face_distance_cm is not None and face_distance_cm > 0:
        v["face_distance_cm"] = round(float(face_distance_cm), 1)
    if face_distance_baseline_cm is not None and face_distance_baseline_cm > 0:
        v["face_distance_baseline_cm"] = round(float(face_distance_baseline_cm), 1)
    _put(v, "blink_rate_baseline", blink_rate_baseline)

    _put(v, "detect_rate", _clamp01(detect_rate))
    _put(v, "yaw_dropped_rate", _clamp01(yaw_dropped_rate))
    _put(v, "calibrating", None if calibrating is None else bool(calibrating))

    return {"v": V, "t": round(float(t), 3), "source": "vision",
            "user_name": user_name, "vision": v}
