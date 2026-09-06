"""
fusion/state.py
───────────────
상태 결정. **순수 함수만 둡니다.**

시계·소켓·DB·파일에 접근하지 않습니다. `now` 는 인자로 받습니다.
그래야 하드웨어 없이 테스트되고, 녹화 로그를 재생해
"임계 A 면 하루 알림 N 회" 같은 표를 뽑을 수 있습니다.

    st = FusionState()
    st, decision = step(st, sample, now)

`decision` 은 docs/contracts/state.schema.json 을 따릅니다.

이 로직은 이전 레포에서 server(app.py) 안에 있었습니다.
분석 담당이 코드를 넣을 자리가 없어 백엔드 파일을 같이 고쳐야 했고,
그래서 여기로 분리했습니다.
"""
from dataclasses import dataclass, field, replace
from typing import Optional

# ── 임계값 ───────────────────────────────────────────────────────────────────
# 전부 추정치입니다. 실측 데이터로 재조정하기 전까지 확정값으로 쓰지 마세요.

OCCUPANCY_MIN     = 100    # 압력 합이 이 값 미만이면 자리 비움
BALANCE_DIFF      = 50     # 좌우 압력차가 이 값을 넘으면 편중
BALANCE_RELEASE   = 30     # 편중 해제 임계 (히스테리시스)

BLINK_RATE_LOW    = 8.0    # 분당 깜빡임이 이 값 미만이면 저깜빡임 (평소값이 없을 때)
BLINK_RATE_OK     = 11.0   # 회복 임계 (히스테리시스)

# 평소값이 오면 절대 임계 대신 **평소 대비 비율**로 봅니다.
# 실측(S01): 평소 21.7회/분. 절대 임계 8 은 이미 한참 진행된 상태라
# 그때까지 아무 일도 일어나지 않습니다. 사람마다 평소가 다릅니다.
BLINK_REL_LOW     = 0.6    # 평소의 60% 미만이면 저깜빡임
BLINK_REL_OK      = 0.8    # 80% 이상 회복하면 해제
LOW_BLINK_CAUTION = 300.0  # 저깜빡임 연속 초 → 주의
LOW_BLINK_RISK    = 900.0  # → 위험

DISTANCE_CLOSE_CM = 45.0   # 이보다 가까우면 근접 (평소값이 없을 때)
DISTANCE_OK_CM    = 50.0   # 회복 임계

# 거리도 같습니다. 실측(S01)의 평소 거리는 35cm 였습니다. 절대 임계 45cm 로는
# 앉는 순간부터 계속 근접 경고입니다. "가까이 앉는 사람" 과 "평소보다 다가온
# 사람" 은 다르고, 이 제품이 잡으려는 것은 후자입니다.
DISTANCE_REL_CLOSE = 0.85  # 평소의 85% 미만(=15% 이상 다가옴)이면 근접
DISTANCE_REL_OK    = 0.92  # 92% 이상 돌아오면 해제

STATIC_EPS        = 25     # 압력 변화량이 이 값 이하면 "안 움직임"
STATIC_CAUTION    = 1200.0 # 정적 유지 초 → 주의
STATIC_RISK       = 2700.0 # → 위험

MAX_GAP_SEC       = 5.0    # 샘플 간격이 이보다 크면 절전·재시작으로 보고 리셋


@dataclass(frozen=True)
class FusionState:
    """누적 상태. 불변 객체이므로 step() 이 새 인스턴스를 돌려줍니다."""
    last_t:          Optional[float] = None
    session_start:   Optional[float] = None
    seated:          bool = False

    low_blink_sec:   float = 0.0   # 연속. 회복하면 0
    static_hold_sec: float = 0.0   # 연속. 움직이면 0
    close_dist_sec:  float = 0.0
    imbalance_sec:   float = 0.0

    static_total:    float = 0.0   # 세션 누적. 리포트용, 리셋 없음
    balance:         str = "CENTER"

    _last_pressure:  tuple = field(default=())


def _blink_thresholds(s):
    """
    (저깜빡임 임계, 회복 임계).

    평소값이 오면 그 비율로, 없으면 절대값으로 봅니다. 절대 임계 8회/분은
    사람마다 의미가 다릅니다 — 평소 22회인 사람에게 8은 이미 한참 진행된
    상태이고, 그때까지 아무 신호도 나가지 않습니다.
    """
    base = s.get("blink_rate_baseline")
    if base and base > 0:
        return base * BLINK_REL_LOW, base * BLINK_REL_OK
    return BLINK_RATE_LOW, BLINK_RATE_OK


def _distance_thresholds(s):
    """
    (근접 임계, 해제 임계).

    "가까이 앉는 사람" 과 "평소보다 다가온 사람" 은 다릅니다. 실측에서 평소
    35cm 에 앉는 사용자가 있었는데, 절대 임계 45cm 로는 앉는 순간부터 계속
    근접 경고입니다. 경고가 상시로 켜져 있으면 그건 정보가 아닙니다.
    """
    base = s.get("face_distance_baseline_cm")
    if base and base > 0:
        return base * DISTANCE_REL_CLOSE, base * DISTANCE_REL_OK
    return DISTANCE_CLOSE_CM, DISTANCE_OK_CM


def _balance(pressure, prev):
    """좌우 균형. 히스테리시스를 둬 경계에서 깜빡이지 않게 합니다."""
    left  = pressure[0] + pressure[2]
    right = pressure[1] + pressure[3]
    diff  = left - right

    if prev == "CENTER":
        if diff >  BALANCE_DIFF: return "LEFT"
        if diff < -BALANCE_DIFF: return "RIGHT"
        return "CENTER"
    if prev == "LEFT":
        return "LEFT" if diff > BALANCE_RELEASE else "CENTER"
    return "RIGHT" if diff < -BALANCE_RELEASE else "CENTER"


def _activity(pressure, prev):
    """직전 샘플 대비 채널 변화량 합."""
    if not prev or len(prev) != len(pressure):
        return float("inf")          # 첫 샘플은 "움직임"으로 봅니다
    return sum(abs(a - b) for a, b in zip(pressure, prev))


def step(st: FusionState, s: dict, now: float):
    """
    Args:
        st:  직전 FusionState
        s:   병합된 최신 관측. 서버가 chair/vision 을 t 로 맞춰 만든 것.
             {"pressure": [4], "ir": [1], "blink_rate": float,
              "face_distance_cm": float, "face_detected": bool}
        now: 현재 시각 (초)

    Returns:
        (새 FusionState, decision dict)
    """
    dt = 0.0 if st.last_t is None else now - st.last_t
    if dt < 0 or dt > MAX_GAP_SEC:
        dt = 0.0                     # 시계 점프는 누적하지 않습니다

    pressure = list(s.get("pressure") or [])
    seated   = len(pressure) == 4 and sum(pressure) >= OCCUPANCY_MIN

    # ── 자리 비움: 연속 누적값을 전부 리셋합니다 ────────────────────────────
    if not seated:
        st2 = replace(
            st, last_t=now, seated=False,
            low_blink_sec=0.0, static_hold_sec=0.0,
            close_dist_sec=0.0, imbalance_sec=0.0,
            balance="CENTER", _last_pressure=(),
        )
        return st2, _decision(st2, s, now, "ABSENT", 1.0, [])

    session_start = st.session_start if st.seated else now

    balance = _balance(pressure, st.balance)
    imbalance_sec = st.imbalance_sec + dt if balance != "CENTER" else 0.0

    moved = _activity(pressure, st._last_pressure) > STATIC_EPS
    static_hold = 0.0 if moved else st.static_hold_sec + dt
    static_total = st.static_total + (0.0 if moved else dt)

    # ── 웹캠: 값이 없으면 누적을 멈추되 리셋하지는 않습니다 ─────────────────
    rate = s.get("blink_rate")
    low_th, ok_th = _blink_thresholds(s)
    if rate is None:
        low_blink = st.low_blink_sec
    elif rate < low_th:
        low_blink = st.low_blink_sec + dt
    elif rate >= ok_th:
        low_blink = 0.0
    else:
        low_blink = st.low_blink_sec          # 두 임계 사이는 유지

    dist = s.get("face_distance_cm")
    close_th, far_th = _distance_thresholds(s)
    if dist is None or dist <= 0:
        close_dist = st.close_dist_sec
    elif dist < close_th:
        close_dist = st.close_dist_sec + dt
    elif dist >= far_th:
        close_dist = 0.0
    else:
        close_dist = st.close_dist_sec

    st2 = replace(
        st, last_t=now, seated=True, session_start=session_start,
        balance=balance, imbalance_sec=imbalance_sec,
        static_hold_sec=static_hold, static_total=static_total,
        low_blink_sec=low_blink, close_dist_sec=close_dist,
        _last_pressure=tuple(pressure),
    )

    # ── 상태 판정 ──────────────────────────────────────────────────────────
    reasons, level = [], 0
    if low_blink >= LOW_BLINK_RISK:      reasons.append("low_blink");      level = max(level, 2)
    elif low_blink >= LOW_BLINK_CAUTION: reasons.append("low_blink");      level = max(level, 1)
    if static_hold >= STATIC_RISK:       reasons.append("static_hold");    level = max(level, 2)
    elif static_hold >= STATIC_CAUTION:  reasons.append("static_hold");    level = max(level, 1)
    if close_dist >= LOW_BLINK_CAUTION:  reasons.append("close_distance"); level = max(level, 1)
    if imbalance_sec >= LOW_BLINK_CAUTION: reasons.append("imbalance");    level = max(level, 1)

    state = ("NORMAL", "CAUTION", "DANGER")[level]

    # 웹캠이 없으면 신뢰도를 낮춥니다. 서버는 confidence < 0.5 면 승격하지 않습니다.
    # 검출률(detect_rate)이 오면 그것을 씁니다. 프레임 하나가 우연히 잡힌 것과
    # 계속 안정적으로 잡히는 것을 불리언 하나로는 구분할 수 없었습니다.
    detect = s.get("detect_rate")
    if detect is not None:
        confidence = round(0.45 + 0.45 * min(max(detect, 0.0), 1.0), 2)
    else:
        confidence = 0.9 if s.get("face_detected") else 0.55
    if rate is None and dist is None:
        confidence = min(confidence, 0.45)

    return st2, _decision(st2, s, now, state, confidence, reasons)


def _decision(st, s, now, state, confidence, reasons):
    score = {"NORMAL": 90, "CAUTION": 60, "DANGER": 30, "ABSENT": 0}[state]
    metrics = {
        "balance":         st.balance,
        "seated":          st.seated,
        "low_blink_sec":   round(st.low_blink_sec, 1),
        "static_hold_sec": round(st.static_hold_sec, 1),
        "session_sec":     round(now - st.session_start, 1) if st.session_start else 0.0,
    }
    # 값이 없으면 키를 생략합니다 — null 은 계약 위반입니다
    # (docs/contracts/README.md 규칙 5). 웹캠이 없을 때 이 dict 는 계속
    # blink_rate: null 을 담고 있었고, 아무도 state 를 검증하지 않아서
    # 드러나지 않았을 뿐입니다.
    for key in ("blink_rate", "face_distance_cm", "blink_rate_baseline",
                "face_distance_baseline_cm", "detect_rate"):
        value = s.get(key)
        if value is not None:
            metrics[key] = value

    return {
        "v": 1,
        "t": round(now, 3),
        "user_name": s.get("user_name", "guest"),
        "state": state,
        "confidence": round(confidence, 2),
        "score": score,
        "reasons": reasons,
        "metrics": metrics,
    }
