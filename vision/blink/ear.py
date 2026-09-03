"""
vision/blink/ear.py
───────────────────
EAR (Eye Aspect Ratio) 로 눈 깜빡임을 셉니다.

**Face Mesh 가 필요합니다.** 이전 레포는 Pose(33점)를 썼는데 거기엔 눈 윤곽이
없어 EAR 을 계산할 수 없습니다. Face Mesh(468점)로 바꿉니다.

        (p2─p6) + (p3─p5)
  EAR = ─────────────────
            2 × (p1─p4)

눈을 뜨면 세로가 크고, 감으면 0 에 가까워집니다.
얼굴이 커지든 작아지든 비율이므로 **카메라 거리에 영향받지 않습니다.**
절대 각도와 달리 이 성질 때문에 깜빡임이 견고한 지표입니다.
"""
import math
from collections import deque

# MediaPipe Face Mesh 눈 윤곽 인덱스
# 순서: [바깥끝, 위1, 위2, 안끝, 아래2, 아래1]
LEFT_EYE  = (362, 385, 387, 263, 373, 380)
RIGHT_EYE = (33, 160, 158, 133, 153, 144)

EAR_CLOSED = 0.21      # 이 값 아래로 내려가면 감은 것으로 봅니다
EAR_OPEN   = 0.25      # 다시 이 값 위로 올라오면 뜬 것 (히스테리시스)
MIN_CLOSED_MS = 60     # 이보다 짧으면 노이즈로 봅니다
MAX_CLOSED_MS = 500    # 이보다 길면 깜빡임이 아니라 감고 있는 것


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def eye_ear(pts, idx):
    p = [pts[i] for i in idx]
    horiz = _dist(p[0], p[3])
    if horiz < 1e-6:
        return 0.0
    return (_dist(p[1], p[5]) + _dist(p[2], p[4])) / (2.0 * horiz)


def face_ear(pts):
    """양쪽 눈 평균. 한쪽이 가려져도 대략 버팁니다."""
    return (eye_ear(pts, LEFT_EYE) + eye_ear(pts, RIGHT_EYE)) / 2.0


class BlinkCounter:
    """
    EAR 시계열 → 깜빡임 사건 + 분당 빈도.

        c = BlinkCounter()
        blinked = c.update(ear, now)     # 이 프레임에서 깜빡임이 끝났으면 True
        rate = c.rate(now)               # 최근 60초 분당 횟수
    """

    def __init__(self, window_sec: float = 60.0):
        self.window = window_sec
        self._closed_since = None
        self._events = deque()

    def update(self, ear: float, now: float) -> bool:
        blinked = False
        if self._closed_since is None:
            if ear < EAR_CLOSED:
                self._closed_since = now
        else:
            if ear > EAR_OPEN:
                ms = (now - self._closed_since) * 1000.0
                if MIN_CLOSED_MS <= ms <= MAX_CLOSED_MS:
                    self._events.append(now)
                    blinked = True
                self._closed_since = None

        cutoff = now - self.window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return blinked

    def rate(self, now: float) -> float:
        """분당 횟수. 관측 시간이 창보다 짧으면 그만큼으로 나눕니다."""
        cutoff = now - self.window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return round(len(self._events) * 60.0 / self.window, 1)
