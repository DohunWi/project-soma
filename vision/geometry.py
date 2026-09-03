"""
vision/geometry.py
──────────────────
Face Mesh 랜드마크 → 얼굴 폭 · 좌우 회전(yaw).

거리는 핀홀 근사로 구합니다.  face_width_px × distance = 상수

**yaw 게이트가 필요한 이유**

고개를 옆으로 돌리면 얼굴 폭이 투영상 줄어듭니다. 보정 없이 쓰면
"멀어졌다" 고 오판합니다. 30도만 돌려도 폭이 13% 줄어 60cm 가 69cm 로 보입니다.

보정 대신 **버리는 쪽**을 택했습니다.
cos 로 나눠 되살리면 노이즈가 증폭되고, 큰 yaw 에서는 랜드마크 자체가
부정확해집니다. 정면일 때만 쓰는 편이 값의 신뢰도가 높습니다.
프레임을 버려도 거리는 초 단위로 변하는 값이라 문제가 없습니다.
"""
import math
import os

NOSE_TIP = 1
FACE_L, FACE_R = 234, 454      # 좌우 얼굴 경계

# 비대칭 비율 허용치. **추정치입니다** — EAR 임계와 마찬가지로 실측이 필요합니다.
# 너무 조이면 프레임을 과하게 버려 거리값이 드문드문해지고,
# 너무 풀면 고개 돌림이 거리 오차로 들어옵니다.
# vision/eval/distance_check.py 로 "고개 돌림 제외" 프레임 수를 보며 조정하세요.
YAW_MAX = float(os.getenv("VISION_YAW_MAX", 0.22))


def face_width_px(pts) -> float:
    lx, ly = pts[FACE_L]
    rx, ry = pts[FACE_R]
    return math.hypot(rx - lx, ry - ly)


def yaw_asymmetry(pts) -> float:
    """
    0 = 정면, 1 = 완전히 옆.

    코 끝에서 좌우 얼굴 경계까지의 거리 비대칭을 봅니다.
    정면이면 두 거리가 같고, 돌릴수록 한쪽이 짧아집니다.
    각도가 아니라 비율이므로 카메라 거리에 영향받지 않습니다.
    """
    nx, ny = pts[NOSE_TIP]
    dl = math.hypot(nx - pts[FACE_L][0], ny - pts[FACE_L][1])
    dr = math.hypot(nx - pts[FACE_R][0], ny - pts[FACE_R][1])
    s = dl + dr
    return abs(dl - dr) / s if s > 1e-6 else 1.0


def is_frontal(pts, yaw_max: float = YAW_MAX) -> bool:
    return yaw_asymmetry(pts) <= yaw_max
