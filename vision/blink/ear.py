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
import sys
from collections import deque
from datetime import datetime, timezone

# MediaPipe Face Mesh 눈 윤곽 인덱스
# 순서: [바깥끝, 위1, 위2, 안끝, 아래2, 아래1]
LEFT_EYE  = (362, 385, 387, 263, 373, 380)
RIGHT_EYE = (33, 160, 158, 133, 153, 144)

EAR_CLOSED = 0.21      # 이 값 아래로 내려가면 감은 것으로 봅니다
EAR_OPEN   = 0.25      # 다시 이 값 위로 올라오면 뜬 것 (히스테리시스)
MIN_CLOSED_MS = 60     # 이보다 짧으면 노이즈로 봅니다
MAX_CLOSED_MS = 500     # 이보다 길면 깜빡임이 아니라 감고 있는 것
RECOVERY_OPEN_MS = 100  # overlong 감김 뒤 연속 OPEN 확인 시간
RECOVERY_OPEN_FRAMES = 3

PHASE_OPEN = "OPEN"
PHASE_CLOSED_CANDIDATE = "CLOSED_CANDIDATE"
PHASE_WAIT_FOR_RECOVERY = "WAIT_FOR_RECOVERY"

# 이보다 적게 관측했으면 분당 빈도를 내보내지 않습니다.
# 10초에 1회 관측한 것을 "분당 6회" 로 환산하면 표본 1개짜리 숫자가 됩니다.
MIN_OBSERVED_SEC = 10.0


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def eye_ear(pts, idx):
    p = [pts[i] for i in idx]
    horiz = _dist(p[0], p[3])
    if horiz < 1e-6:
        return 0.0
    return (_dist(p[1], p[5]) + _dist(p[2], p[4])) / (2.0 * horiz)


def face_ear_values(pts):
    """Return left, right, and the exact combined EAR used for detection."""
    left = eye_ear(pts, LEFT_EYE)
    right = eye_ear(pts, RIGHT_EYE)
    return left, right, (left + right) / 2.0


def face_ear(pts):
    """양쪽 눈 평균. 한쪽이 가려져도 대략 버팁니다."""
    return face_ear_values(pts)[2]


class BlinkCounter:
    """
    EAR 시계열 → 깜빡임 사건 + 분당 빈도.

    500ms를 넘긴 감김은 WAIT_FOR_RECOVERY에서 연속 OPEN evidence를
    확인한 뒤 새 candidate를 받을 수 있습니다. Face loss는 진행 상태를
    명시적으로 폐기해야 하므로 runtime은 on_face_lost()를 호출합니다.

        c = BlinkCounter()
        blinked = c.update(ear, now)     # 이 프레임에서 깜빡임이 끝났으면 True
        rate = c.rate(now)               # 최근 60초 분당 횟수
    """

    def __init__(
        self,
        window_sec: float = 60.0,
        *,
        closed_threshold=None,
        open_threshold=None,
        recovery_open_ms: float = RECOVERY_OPEN_MS,
        recovery_open_frames: int = RECOVERY_OPEN_FRAMES,
    ):
        self.closed_threshold = (
            EAR_CLOSED if closed_threshold is None else float(closed_threshold)
        )
        self.open_threshold = (
            EAR_OPEN if open_threshold is None else float(open_threshold)
        )
        if not self.open_threshold > self.closed_threshold:
            raise ValueError("open_threshold must be greater than closed_threshold")
        if recovery_open_ms <= 0:
            raise ValueError("recovery_open_ms must be positive")
        if recovery_open_frames < 2:
            raise ValueError("recovery_open_frames must be at least 2")

        self.window = window_sec
        self.recovery_open_ms = float(recovery_open_ms)
        self.recovery_open_frames = int(recovery_open_frames)
        self._phase = PHASE_OPEN
        self._closed_since = None
        self._recovery_open_since = None
        self._recovery_open_frame_count = 0
        self._events = deque()
        self._t0 = None              # 첫 관측 시각. 창이 덜 찼을 때 환산에 씁니다
        self.last_duration_ms = None # 직전 깜빡임에서 눈을 감고 있던 시간
        self.last_transition_reason = None

    def update(self, ear: float, now: float) -> bool:
        if self._t0 is None:
            self._t0 = now

        self.last_transition_reason = None
        blinked = False
        if self._phase == PHASE_OPEN:
            if ear < self.closed_threshold:
                self._phase = PHASE_CLOSED_CANDIDATE
                self._closed_since = now
                self.last_transition_reason = "closed_threshold"

        elif self._phase == PHASE_CLOSED_CANDIDATE:
            duration_ms = (now - self._closed_since) * 1000.0
            if duration_ms > MAX_CLOSED_MS:
                self._phase = PHASE_WAIT_FOR_RECOVERY
                self._closed_since = None
                self._recovery_open_since = (
                    now if ear > self.open_threshold else None
                )
                self._recovery_open_frame_count = (
                    1 if ear > self.open_threshold else 0
                )
                self.last_transition_reason = "timeout"
            elif ear > self.open_threshold:
                ms = duration_ms
                if MIN_CLOSED_MS <= ms <= MAX_CLOSED_MS:
                    self._events.append(now)
                    self.last_duration_ms = round(ms, 1)
                    blinked = True
                    self.last_transition_reason = "blink"
                else:
                    self.last_transition_reason = "noise"
                self._phase = PHASE_OPEN
                self._closed_since = None

        else:  # WAIT_FOR_RECOVERY
            if ear > self.open_threshold:
                if self._recovery_open_since is None:
                    self._recovery_open_since = now
                    self._recovery_open_frame_count = 1
                else:
                    self._recovery_open_frame_count += 1
                if (
                    self._recovery_open_frame_count >= self.recovery_open_frames
                    and (now - self._recovery_open_since) * 1000.0
                    >= self.recovery_open_ms
                ):
                    self._phase = PHASE_OPEN
                    self._recovery_open_since = None
                    self._recovery_open_frame_count = 0
                    self.last_transition_reason = "recovery"
            else:
                # Hysteresis/closed frames break the stable-open evidence.
                self._recovery_open_since = None
                self._recovery_open_frame_count = 0

        self._prune(now)
        return blinked

    def on_face_lost(self) -> bool:
        """Invalidate in-flight state so it cannot continue after face recovery."""
        self.last_transition_reason = None
        if self._phase == PHASE_OPEN:
            return False
        self._phase = PHASE_OPEN
        self._closed_since = None
        self._recovery_open_since = None
        self._recovery_open_frame_count = 0
        self.last_transition_reason = "face_loss"
        return True

    @property
    def phase(self) -> str:
        """Current state-machine phase exposed for standalone diagnostics."""
        return self._phase

    def candidate_duration_ms(self, now: float):
        """Elapsed candidate duration, or None while no closed candidate exists."""
        if self._closed_since is None:
            return None
        return max((now - self._closed_since) * 1000.0, 0.0)

    def recovery_open_duration_ms(self, now: float):
        """Current stable-open evidence in WAIT_FOR_RECOVERY, if any."""
        if self._recovery_open_since is None:
            return None
        return max((now - self._recovery_open_since) * 1000.0, 0.0)

    @property
    def recovery_open_frame_count(self) -> int:
        return self._recovery_open_frame_count

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def observed_sec(self, now: float) -> float:
        """실제로 관측한 시간. 창(60초)을 넘지 않습니다."""
        if self._t0 is None:
            return 0.0
        return min(max(now - self._t0, 0.0), self.window)

    def count(self, now: float) -> int:
        """창 안의 깜빡임 사건 수."""
        self._prune(now)
        return len(self._events)

    def rate(self, now: float):
        """
        분당 횟수. **관측한 시간으로 나눕니다** — 창 길이가 아니라.

        이전에는 항상 60 으로 나눠, 시작 직후에는 실제 분당 20회가 5.0 으로
        보고됐습니다. 그 값이 임계(8) 아래라 세션마다 저깜빡임 누적이
        시작됐습니다.

        관측이 MIN_OBSERVED_SEC 보다 짧으면 **None** 을 돌려줍니다.
        표본이 1~2개일 때의 환산은 분산이 커서 숫자로 내보내면 안 됩니다.
        받는 쪽은 값이 없으면 누적을 멈추되 리셋하지 않습니다
        (fusion/state.py 의 blink_rate is None 분기).
        """
        obs = self.observed_sec(now)
        if obs < MIN_OBSERVED_SEC:
            return None
        return round(self.count(now) * 60.0 / obs, 1)


def blink_diagnostic_line(
    *,
    now,
    left_ear,
    right_ear,
    combined_ear,
    phase_before,
    counter,
    blinked,
):
    """Build one human-readable line without changing the sensor_data contract."""
    timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if combined_ear is None:
        eye_state = "NO_FACE"
    elif combined_ear < counter.closed_threshold:
        eye_state = "CLOSED"
    elif combined_ear > counter.open_threshold:
        eye_state = "OPEN"
    else:
        eye_state = "HYSTERESIS"

    transition = (
        counter.phase
        if phase_before == counter.phase
        else f"{phase_before}->{counter.phase}"
    )
    candidate_ms = counter.candidate_duration_ms(now)
    recovery_open_ms = counter.recovery_open_duration_ms(now)
    duration_ms = counter.last_duration_ms if blinked else None

    def metric(value):
        return "NA" if value is None else f"{value:.4f}"

    return (
        f"[blink-debug] timestamp={timestamp} "
        f"left_ear={metric(left_ear)} right_ear={metric(right_ear)} "
        f"combined_ear={metric(combined_ear)} "
        f"threshold_closed=<{counter.closed_threshold:.3f} "
        f"threshold_open=>{counter.open_threshold:.3f} "
        f"eye={eye_state} state={counter.phase} transition={transition} "
        f"reason={counter.last_transition_reason or 'none'} "
        f"candidate_ms={'NA' if candidate_ms is None else f'{candidate_ms:.1f}'} "
        f"recovery_open_ms="
        f"{'NA' if recovery_open_ms is None else f'{recovery_open_ms:.1f}'} "
        f"recovery_open_frames={counter.recovery_open_frame_count} "
        f"event={str(bool(blinked)).lower()} "
        f"duration_ms={'NA' if duration_ms is None else f'{duration_ms:.1f}'}"
    )


def emit_blink_diagnostic(enabled, *, stream=None, **values):
    """Write diagnostics only when explicitly enabled; return the emitted line."""
    if not enabled:
        return None
    line = blink_diagnostic_line(**values)
    print(line, file=stream or sys.stderr, flush=True)
    return line
