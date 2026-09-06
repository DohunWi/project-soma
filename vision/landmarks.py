"""
vision/landmarks.py
───────────────────
얼굴 랜드마크 공급자. **mediapipe 를 import 하는 유일한 파일입니다.**

mediapipe 가 레거시 Solutions API 를 없앴습니다. 현재 배포판(0.10.35 · 1.0.1)
에는 `mp.solutions` 자체가 없어서, 이 API 를 직접 부르던 run.py ·
eval/record.py · eval/distance_check.py 가 새로 만든 환경에서 **import 단계에
죽습니다.** 세 파일이 같은 API 를 각자 부르고 있어 같은 수정을 세 번 해야
했습니다. 이제 캡처 계층은 이 파일만 봅니다.

돌려주는 것은 mediapipe 객체가 아니라 **{인덱스: (x_px, y_px)} dict** 입니다.
`geometry.py` 와 `blink/ear.py` 가 이미 그 형식을 기대하고, 그래야 두 모듈이
mediapipe 를 모른 채로 테스트됩니다.

랜드마크 번호는 그대로입니다 — Tasks 의 FaceLandmarker 도 Face Mesh 와 같은
468점(+홍채 10점) 토폴로지를 씁니다. EAR·얼굴폭 인덱스를 고칠 필요가 없습니다.

    det = FaceLandmarks()
    pts = det.detect(frame_bgr, now)      # dict 또는 None
    det.close()

모델 파일은 저장소에 넣지 않습니다(3.7MB). 없으면 내려받아
`vision/models/` 에 캐시합니다. `.env` 의 `VISION_MODEL_PATH` 로 경로를
직접 줄 수도 있습니다 — 발표장 네트워크가 막힐 수 있으므로 미리 받아 두세요.
"""
import os
import sys
import urllib.request
from pathlib import Path

# numpy · mediapipe 는 클래스 안에서 import 합니다. 이 모듈을 import 하는 것만으로
# 무거운 의존성이 필요하면, 계약·순수 로직 테스트가 웹캠 의존성 없이는
# 못 돌게 됩니다 (그게 이 어댑터를 만든 이유입니다).

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MODEL_DIR = Path(__file__).parent / "models"
MODEL_NAME = "face_landmarker.task"


def model_path(explicit=None) -> Path:
    """지정 → .env → 기본 캐시 경로 순."""
    return Path(explicit or os.getenv("VISION_MODEL_PATH") or (MODEL_DIR / MODEL_NAME))


def ensure_model(path=None) -> Path:
    """모델이 없으면 내려받습니다. 있으면 그대로 씁니다."""
    p = model_path(path)
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    print(f"[landmarks] 모델을 내려받습니다 (3.7MB) → {p}", file=sys.stderr)
    tmp = p.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)      # noqa: S310
    tmp.replace(p)                                   # 부분 파일을 캐시로 오해하지 않도록
    return p


class MonotonicMs:
    """
    Tasks 의 VIDEO 모드는 **단조 증가하는** ms 타임스탬프를 요구합니다.
    같은 값이 두 번 오면 예외가 납니다. 30fps 여도 프레임 두 장이 같은
    밀리초에 들어오는 일은 실제로 생기므로 여기서 막습니다.

    mediapipe 없이 테스트할 수 있도록 따로 뺐습니다.
    """

    def __init__(self):
        self._t0 = None
        self._last = -1

    def ms(self, t=None) -> int:
        if t is None:
            value = self._last + 1
        else:
            if self._t0 is None:
                self._t0 = t
            value = int((t - self._t0) * 1000.0)
        value = max(value, self._last + 1)
        self._last = value
        return value


class FaceLandmarks:
    """
    Tasks API(FaceLandmarker)를 씁니다. 레거시 Solutions 가 남아 있는 옛
    설치본에서는 그쪽으로 자동 전환합니다 — 팀원이 이미 깔아 둔 환경을
    깨지 않기 위해서입니다.
    """

    def __init__(self, model=None, max_faces: int = 1, video: bool = True):
        import mediapipe as mp                       # 여기서만 import 합니다
        import numpy as np

        self.backend = None
        self._mp = mp
        self._np = np
        self._clock = MonotonicMs()

        if hasattr(mp, "tasks"):
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision as mpv

            mode = mpv.RunningMode.VIDEO if video else mpv.RunningMode.IMAGE
            # delegate 를 CPU 로 못박습니다. 기본값은 GPU 경로로 가는데,
            # macOS(M2)에서 Metal 헬퍼가 초기화되지 않으면 파이썬 예외가 아니라
            # 프로세스 abort 로 죽습니다 — 시연 중이면 원인을 못 찾습니다.
            opts = mpv.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(ensure_model(model)),
                                         delegate=BaseOptions.Delegate.CPU),
                running_mode=mode, num_faces=max_faces,
                output_face_blendshapes=False, output_facial_transformation_matrixes=False)
            self._det = mpv.FaceLandmarker.create_from_options(opts)
            self._video = video
            self.backend = "tasks"

        elif hasattr(mp, "solutions"):                # 옛 설치본 (0.10.x 초기)
            self._det = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=max_faces, refine_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self.backend = "solutions"

        else:
            raise RuntimeError(
                "mediapipe 에 tasks 도 solutions 도 없습니다. "
                "설치를 확인하세요:  pip install -r vision/requirements.txt")

    # ── 검출 ─────────────────────────────────────────────────────────
    def detect(self, frame_bgr, t: float = None):
        """
        Args:
            frame_bgr: OpenCV 프레임 (BGR, uint8)
            t:         측정 시각(초). Tasks 의 VIDEO 모드는 단조 증가하는
                       타임스탬프를 요구합니다

        Returns:
            {인덱스: (x_px, y_px)} 또는 얼굴이 없으면 None
        """
        h, w = frame_bgr.shape[:2]

        if self.backend == "solutions":
            rgb = self._np.ascontiguousarray(frame_bgr[:, :, ::-1])
            res = self._det.process(rgb)
            if not res.multi_face_landmarks:
                return None
            lm = res.multi_face_landmarks[0].landmark
            return {i: (lm[i].x * w, lm[i].y * h) for i in range(len(lm))}

        mp_img = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=self._np.ascontiguousarray(frame_bgr[:, :, ::-1]))

        if self._video:
            res = self._det.detect_for_video(mp_img, self._clock.ms(t))
        else:
            res = self._det.detect(mp_img)

        if not res.face_landmarks:
            return None
        lm = res.face_landmarks[0]
        return {i: (p.x * w, p.y * h) for i, p in enumerate(lm)}

    def close(self):
        self._det.close()


if __name__ == "__main__":
    # 발표 전에 미리 받아 두는 용도입니다.
    #   python vision/landmarks.py
    # 발표장 네트워크가 막히면 그 자리에서 받을 수 없습니다.
    path = ensure_model()
    print(f"모델 준비 완료: {path}  ({path.stat().st_size / 1e6:.1f}MB)")
