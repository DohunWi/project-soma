"""
vision/distance.py
──────────────────
얼굴 ↔ 화면 거리 추정.

**개인 캘리브레이션 없이 절대 거리를 냅니다.**

    거리_cm = f_px × 홍채지름_cm / 홍채지름_px

  f_px      카메라 고유값. 화각에서 유도하거나(자 불필요) 자로 한 번 재서 고정
  홍채지름  사람 몸에 있는 기준자. 성인 11.7mm ± 0.5mm 로 개인차가 작다

이전 방식은 "3초 동안 앉아 있는 그 순간이 60cm" 라고 **가정**했습니다.
아무도 자로 재지 않으므로 거리 전체가 임의의 배율로 어긋났습니다. 실측에서
같은 사람이 70cm 로도 77cm 로도 나왔는데, 어느 쪽이 맞는지 알 방법이
없었습니다. fusion 의 근접 임계 45cm 는 그 위에서 판정하고 있었습니다.

얼굴 폭을 쓰지 않는 이유: 개인차가 ±5~8% 로 홍채(±4%)보다 크고, 고개를
돌리면 투영 폭이 줄어 거리 오차로 들어옵니다. 홍채는 원형이라 각도에
덜 민감합니다.

캘리브레이터는 이제 스케일을 정하지 않습니다. 그 사람의 **평소 거리**를
실제로 재서 "평소보다 N cm 가까움" 을 만들 수 있게 합니다.
"""
import os

from geometry import (IRIS_DIAMETER_CM, distance_cm_from_iris,  # noqa: F401
                      face_width_px, fov_deg_from_focal, iris_px, resolve_focal_px)

# 노트북 내장캠의 흔한 가로 화각. 정확한 값은 카메라마다 다르므로
# 자로 한 번 잰 VISION_FOCAL_PX 가 있으면 그쪽이 항상 이깁니다.
DEFAULT_FOV_DEG = 55.0


class DistanceEstimator:
    """
    홍채로 거리를 재고, 홍채가 없으면 얼굴 폭 + 개인 baseline 으로 버팁니다.

        est = DistanceEstimator(image_width_px=1920)
        cm, source = est.distance_cm(pts)      # source: iris | calib | None
    """

    def __init__(self, image_width_px=None, focal_px=None, fov_deg=None,
                 calibrator=None):
        self.image_width_px = image_width_px
        self.calibrator = calibrator
        focal_px = focal_px if focal_px is not None else os.getenv("VISION_FOCAL_PX")
        fov_deg = fov_deg if fov_deg is not None else os.getenv("VISION_CAMERA_FOV_DEG")
        self.focal_px, self.focal_source = resolve_focal_px(
            image_width_px or 0, focal_px, fov_deg or DEFAULT_FOV_DEG)

    def describe(self) -> str:
        if not self.focal_px:
            return "초점거리 없음 — 개인 캘리브레이션으로 대체합니다"
        how = {"measured": ".env 의 VISION_FOCAL_PX", "fov": "화각에서 유도"}[self.focal_source]
        fov = fov_deg_from_focal(self.image_width_px or 0, self.focal_px)
        return (f"f_px={self.focal_px:.0f} ({how}"
                + (f", 화각 {fov:.0f}°" if fov else "") + ")")

    def distance_cm(self, pts):
        """(거리_cm, 근거). 못 구하면 (None, None) — 값이 없으면 키를 생략합니다."""
        if pts is None:
            return None, None

        if self.focal_px:
            ip = iris_px(pts)
            if ip:
                cm = distance_cm_from_iris(ip, self.focal_px)
                if cm and 15.0 < cm < 200.0:     # 사람이 앉을 수 있는 범위 밖이면 버립니다
                    return round(cm, 1), "iris"

        # 홍채를 못 잡는 모델·해상도에서의 대비책입니다.
        if self.calibrator is not None:
            cm = self.calibrator.distance_cm(face_width_px(pts))
            if cm:
                return cm, "calib"
        return None, None
