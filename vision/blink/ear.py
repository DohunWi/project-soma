"""
vision/blink/ear.py
───────────────────
EAR (Eye Aspect Ratio) 로 눈 깜빡임을 셉니다.

**Face Mesh 가 필요합니다.** 이전 레포는 Pose(33점)를 썼는데 거기엔 눈 윤곽이
없어 EAR 을 계산할 수 없습니다. 얼굴 468점 랜드마크를 씁니다.

        (p2─p6) + (p3─p5)
  EAR = ─────────────────
            2 × (p1─p4)

눈을 뜨면 세로가 크고, 감으면 0 에 가까워집니다.
얼굴이 커지든 작아지든 비율이므로 **카메라 거리에 영향받지 않습니다.**

── 임계는 절대값이 아니라 그 사람의 평소 EAR 대비 비율입니다 ──

문헌에서 흔히 쓰는 0.21 / 0.25 를 그대로 박아 뒀더니 실측에서 **깜빡임이 한
번도 검출되지 않았습니다.** 피험자 S01 의 평소 EAR 은 0.22 이고, 회복 임계
0.25 는 그보다 높습니다. 상태기계가 "감김" 에서 빠져나오지 못해 분당 0회를
영원히 보고합니다. 숫자는 나오므로 실사용에서는 고장인 줄 모릅니다.

같은 사람도 자세·조명·시간에 따라 평소 EAR 이 달랐습니다 (0.229 / 0.200).
눈꺼풀 모양·안경·카메라 각도까지 더하면 사람마다는 더 벌어집니다.
그래서 **최근 30초의 평소 EAR 을 계속 추정하고, 임계를 그 비율로 둡니다.**
(architecture.md 4-3: "변화량·비율을 쓰고 절대값을 쓰지 않는다")

실측 근거는 vision/eval/README.md 를 보세요.
"""
import math
from collections import deque

# MediaPipe Face Mesh 눈 윤곽 인덱스
# 순서: [바깥끝, 위1, 위2, 안끝, 아래2, 아래1]
LEFT_EYE  = (362, 385, 387, 263, 373, 380)
RIGHT_EYE = (33, 160, 158, 133, 153, 144)

# ── 임계 (평소 EAR 대비 비율) ────────────────────────────────────────────
# S01 실측 (n=1, 90초 신호 + 60초 자연):
#     신호 재현율 1.000 (19/19)   오검출 0.00/분   자연 22.0회/분 (기준 21.0)
#
# 0.55 도 같은 성적이지만 S01 의 가장 얕은 자연 깜빡임이 평소의 정확히 55%
# 까지만 내려갔습니다. 경계에 걸칩니다. 이 제품은 "깜빡임이 줄어든 것" 을
# 잡는 것이 목적이므로 놓치는 쪽이 더 위험해서 여유를 뒀습니다.
R_CLOSED = 0.65        # 평소의 65% 아래로 내려가면 감은 것으로 봅니다
R_OPEN   = 0.75        # 75% 위로 돌아오면 뜬 것 (히스테리시스)

# baseline 이 아직 없을 때만 쓰는 절대값. S01 실측 최적값입니다.
EAR_CLOSED = 0.14
EAR_OPEN   = 0.17

MIN_CLOSED_MS = 60     # 이보다 짧으면 노이즈로 봅니다
# 400ms 입니다. 500 이면 자연 깜빡임이 26회/분으로 부풀었습니다 (기준 21).
# 눈을 오래 감고 있는 구간이 깜빡임으로 섞여 들어옵니다.
MAX_CLOSED_MS = 400

BASELINE_WINDOW_SEC = 30.0   # 평소 EAR 을 추정하는 창
BASELINE_MIN_SAMPLES = 15    # 이보다 적으면 추정하지 않습니다 (1초 분량)

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


def face_ear(pts):
    """양쪽 눈 평균. 한쪽이 가려져도 대략 버팁니다."""
    return (eye_ear(pts, LEFT_EYE) + eye_ear(pts, RIGHT_EYE)) / 2.0


class BlinkCounter:
    """
    EAR 시계열 → 깜빡임 사건 + 분당 빈도.

        c = BlinkCounter()
        blinked = c.update(ear, now)     # 이 프레임에서 깜빡임이 끝났으면 True
        rate = c.rate(now)               # 관측한 시간 기준 분당 횟수 (없으면 None)
    """

    def __init__(self, window_sec: float = 60.0,
                 r_closed: float = R_CLOSED, r_open: float = R_OPEN,
                 min_ms: float = MIN_CLOSED_MS, max_ms: float = MAX_CLOSED_MS,
                 baseline_window_sec: float = BASELINE_WINDOW_SEC):
        self.window = window_sec
        self.r_closed = r_closed
        self.r_open = r_open
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.baseline_window = baseline_window_sec

        self._closed_since = None
        self._events = deque()
        self._ear_hist = deque()     # (t, ear) — baseline 추정용
        self._t0 = None
        self.last_duration_ms = None
        self.baseline_ear = None     # 마지막으로 추정한 평소 EAR (진단용)

    # ── baseline ─────────────────────────────────────────────────────
    def _update_baseline(self, now: float):
        """
        평소(눈 뜬) EAR 추정 = 최근 창의 **상위 절반의 중앙값**.

        단순 중앙값을 쓰면 깜빡임이 잦을 때 baseline 이 같이 내려가
        임계도 따라 내려갑니다. 그러면 깜빡일수록 덜 세게 됩니다.
        상위 절반만 보면 감긴 구간이 빠집니다.
        """
        cutoff = now - self.baseline_window
        while self._ear_hist and self._ear_hist[0][0] < cutoff:
            self._ear_hist.popleft()
        if len(self._ear_hist) < BASELINE_MIN_SAMPLES:
            return None
        vals = sorted(v for _, v in self._ear_hist)
        upper = vals[len(vals) // 2:]
        self.baseline_ear = round(upper[len(upper) // 2], 4)
        return self.baseline_ear

    def thresholds(self):
        """(감김 임계, 뜸 임계). baseline 이 없으면 실측 절대값으로 버팁니다."""
        if self.baseline_ear is None:
            return EAR_CLOSED, EAR_OPEN
        return self.r_closed * self.baseline_ear, self.r_open * self.baseline_ear

    # ── 상태기계 ─────────────────────────────────────────────────────
    def update(self, ear: float, now: float) -> bool:
        if self._t0 is None:
            self._t0 = now
        self._ear_hist.append((now, ear))
        self._update_baseline(now)
        closed_th, open_th = self.thresholds()

        blinked = False
        if self._closed_since is None:
            if ear < closed_th:
                self._closed_since = now
        else:
            if ear > open_th:
                ms = (now - self._closed_since) * 1000.0
                if self.min_ms <= ms <= self.max_ms:
                    self._events.append(now)
                    self.last_duration_ms = round(ms, 1)
                    blinked = True
                self._closed_since = None

        self._prune(now)
        return blinked

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
        받는 쪽은 값이 없으면 누적을 멈추되 리셋하지 않습니다
        (fusion/state.py 의 blink_rate is None 분기).
        """
        obs = self.observed_sec(now)
        if obs < MIN_OBSERVED_SEC:
            return None
        return round(self.count(now) * 60.0 / obs, 1)


def detect_blinks(frames, r_closed=R_CLOSED, r_open=R_OPEN,
                  min_ms=MIN_CLOSED_MS, max_ms=MAX_CLOSED_MS):
    """
    (t, ear) 목록 → 깜빡임 종료 시각 목록.

    **분석 도구는 이 함수를 씁니다.** 스윕·재생기가 검출기를 따로 구현하면
    분석 결과가 실제 동작을 설명하지 못합니다. 실제로 sweep.py 가 자기
    상태기계를 갖고 있었고, 여기에 baseline 추정이 들어오면서 갈라졌습니다.
    """
    counter = BlinkCounter(r_closed=r_closed, r_open=r_open,
                           min_ms=min_ms, max_ms=max_ms)
    out = []
    for t, ear in frames:
        if ear is None:
            continue
        if counter.update(ear, t):
            out.append(t)
    return out
