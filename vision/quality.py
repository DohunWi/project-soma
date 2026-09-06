"""
vision/quality.py
─────────────────
프레임 품질 집계 — 최근 창의 **얼굴 검출률**과 **고개 돌림 폐기율**.

지금까지 서버가 받은 신뢰도 정보는 `face_detected` 불리언 하나였고,
fusion 은 그것만 보고 confidence 를 0.9 / 0.55 로 찍었습니다.
프레임 하나가 우연히 잡힌 것인지 계속 안정적으로 잡히는 것인지 구분할 수
없었습니다.

거리값이 비는 이유도 세 가지입니다 — 얼굴 없음 / 고개 돌림 / 캘리브레이션 중.
받는 쪽(대시보드)은 "왜 값이 없는지" 를 사용자에게 설명해야 하는데
그 정보가 계약에 없었습니다. 여기서 비율로 만들어 실어 보냅니다.

**순수 클래스입니다.** 시계에 접근하지 않고 `now` 를 인자로 받습니다.

    q = FrameQuality()
    q.update(now, detected=True, frontal=False)
    q.detect_rate(now)        # 0~1 또는 None (표본 없음)
    q.yaw_dropped_rate(now)   # 검출된 프레임 중 고개 돌림으로 버린 비율
"""
from collections import deque

WINDOW_SEC = 10.0      # 이 창으로 비율을 냅니다. 거리는 초 단위로 변하는 값입니다


class FrameQuality:
    def __init__(self, window_sec: float = WINDOW_SEC):
        self.window = window_sec
        self._frames = deque()          # (t, detected, dropped_by_yaw)

    def update(self, now: float, detected: bool, frontal=None) -> None:
        """
        Args:
            detected: 이 프레임에서 얼굴이 검출됐는가
            frontal:  검출됐을 때 정면이었는가. 검출 안 됐으면 None
        """
        dropped = bool(detected) and frontal is False
        self._frames.append((now, bool(detected), dropped))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    def frames(self, now: float) -> int:
        self._prune(now)
        return len(self._frames)

    def detect_rate(self, now: float):
        """창 안에서 얼굴이 검출된 프레임 비율. 표본이 없으면 None."""
        self._prune(now)
        if not self._frames:
            return None
        hit = sum(1 for _, d, _ in self._frames if d)
        return round(hit / len(self._frames), 3)

    def yaw_dropped_rate(self, now: float):
        """
        **검출된 프레임 중** 고개 돌림으로 거리값을 버린 비율.

        분모를 전체 프레임이 아니라 검출 프레임으로 둡니다. 자리를 비우면
        분모가 커져 폐기율이 0 으로 희석되는데, 그러면 "고개를 자주 돌려서
        거리가 안 잡힌다" 는 신호가 사라집니다.
        """
        self._prune(now)
        det = [f for f in self._frames if f[1]]
        if not det:
            return None
        return round(sum(1 for _, _, dr in det if dr) / len(det), 3)
