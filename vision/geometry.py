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


# ── 홍채 ─────────────────────────────────────────────────────────────────────
# Face Mesh 홍채 랜드마크 (refine_landmarks / FaceLandmarker 의 468~477).
# 순서: [중심, 오른쪽, 위, 왼쪽, 아래]
LEFT_IRIS  = (473, 474, 475, 476, 477)
RIGHT_IRIS = (468, 469, 470, 471, 472)

# 사람 홍채의 수평 지름. **개인차가 작습니다** — 성인 11.7mm ± 0.5mm 로
# 성별·인종·나이에 거의 무관합니다. 얼굴 폭은 ±5~8% 인데 홍채는 ±4% 입니다.
# 그래서 "화면 속 크기 → 실제 거리" 의 기준자로 홍채를 씁니다.
IRIS_DIAMETER_CM = 1.17


def iris_diameter_px(pts, idx) -> float:
    """
    홍채의 **수평** 지름(px).

    세로가 아니라 가로를 씁니다. 위아래는 눈꺼풀에 자주 가려지는데
    좌우는 눈을 반쯤 감아도 남습니다.
    """
    _, right, _, left, _ = idx
    return math.hypot(pts[right][0] - pts[left][0], pts[right][1] - pts[left][1])


def iris_px(pts):
    """양쪽 홍채 지름의 평균. 한쪽이 이상하면 큰 쪽을 버리지 않고 평균냅니다."""
    try:
        left = iris_diameter_px(pts, LEFT_IRIS)
        right = iris_diameter_px(pts, RIGHT_IRIS)
    except KeyError:
        return None                 # 홍채 랜드마크가 없는 모델
    if left <= 0 or right <= 0:
        return None
    return (left + right) / 2.0


def focal_px_from_iris(iris_diameter_px_value: float, distance_cm: float) -> float:
    """
    자로 잰 거리 한 번으로 **카메라의 초점거리(px)** 를 구합니다.

        iris_px = f_px x IRIS_DIAMETER_CM / distance_cm

    이 값은 카메라 고유값이라 사람이 바뀌어도 그대로입니다.
    사람마다 캘리브레이션하던 것을 카메라마다 한 번으로 바꾸는 것이 핵심입니다.
    """
    return iris_diameter_px_value * distance_cm / IRIS_DIAMETER_CM


def distance_cm_from_iris(iris_diameter_px_value: float, focal_px: float):
    """홍채 크기 → 거리. 개인 캘리브레이션이 필요 없습니다."""
    if not iris_diameter_px_value or iris_diameter_px_value <= 0 or not focal_px:
        return None
    return focal_px * IRIS_DIAMETER_CM / iris_diameter_px_value


def face_width_cm_from_iris(face_width_px_value: float, iris_diameter_px_value: float):
    """
    그 사람의 실제 얼굴 폭(cm).

    두 값 모두 같은 거리에서 같은 렌즈로 찍혔으므로, 비율만 남고 거리와
    초점거리가 소거됩니다. **자도 카메라 정보도 필요 없습니다.**
    """
    if not iris_diameter_px_value or iris_diameter_px_value <= 0:
        return None
    return IRIS_DIAMETER_CM * face_width_px_value / iris_diameter_px_value


def focal_px_from_fov(image_width_px: float, fov_deg: float) -> float:
    """
    카메라 화각에서 초점거리(px)를 구합니다. **자가 필요 없습니다.**

        f_px = (가로_해상도 / 2) / tan(가로화각 / 2)

    얼굴의 가로와 세로를 둘 다 재도 거리는 나오지 않습니다. 둘 다 거리에
    반비례해 같이 줄어들어서, 비율에서는 스케일이 소거되고 "얼굴 모양" 만
    남습니다. 스케일을 주는 것은 카메라 화각입니다.

    스펙값을 쓰면 그만큼 오차가 통째로 들어옵니다(크롭·디지털 줌이 걸리면
    실제 화각이 달라집니다). 자로 한 번 잰 VISION_FOCAL_PX 가 있으면
    그쪽이 항상 우선입니다.
    """
    if image_width_px <= 0 or not (10.0 < fov_deg < 170.0):
        return None
    return (image_width_px / 2.0) / math.tan(math.radians(fov_deg) / 2.0)


def fov_deg_from_focal(image_width_px: float, focal_px: float) -> float:
    """반대 방향. 측정한 f_px 가 현실적인 화각인지 확인할 때 씁니다."""
    if not focal_px:
        return None
    return 2.0 * math.degrees(math.atan((image_width_px / 2.0) / focal_px))


def resolve_focal_px(image_width_px: float, focal_px=None, fov_deg=None):
    """
    초점거리 결정 순서:  자로 잰 값 → 화각에서 유도 → 없음.

    개인 캘리브레이션과 달리 이 값은 **카메라 고유값**이라 사람이 바뀌어도
    유효합니다. 한 번 재두면 팀원 전체가 씁니다.
    """
    if focal_px:
        return float(focal_px), "measured"
    if fov_deg:
        f = focal_px_from_fov(image_width_px, float(fov_deg))
        if f:
            return f, "fov"
    return None, "none"
