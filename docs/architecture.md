# Project Soma — 마스터 문서

> 이 문서가 프로젝트의 단일 기준입니다.
> 기획·아키텍처·범위·결정이 여기 모여 있고, 다른 문서는 여기서 갈라져 나갑니다.
> 슬라이드와 이 문서가 다르면 **이 문서가 맞습니다.**

---

## 0. 한 줄 정의

> **장시간 컴퓨터 사용자의 피로도를 의자와 웹캠으로 측정하고,
> 작업을 방해하지 않는 방식으로 알려주는 시스템.**

"자세를 교정한다" 가 아니라 **"피로가 쌓이는 것을 본인이 알게 한다"** 입니다.
이 구분은 표현 문제가 아니라 무엇을 측정하고 무엇을 주장할지를 가릅니다.

---

## 1. 문제

장시간 컴퓨터 사용으로 세 가지가 누적됩니다.

| | 무엇 | 왜 본인이 모르는가 |
|---|---|---|
| **안구 건조** | 모니터를 볼 때 눈 깜빡임이 현저히 줄어듦 | 깜빡임은 무의식이라 줄어드는 것을 자각하지 못함 |
| **만성 피로** | 장시간 집중으로 피로 누적 | 몰입 중에는 피로를 인지하지 못함 |
| **목·허리 부담** | 상체가 앞으로 나오고 하중이 한쪽으로 쏠림 | 서서히 바뀌므로 순간을 알아채지 못함 |

공통점: **사용자가 실시간으로 자각하지 못합니다.** 측정과 알림이 값어치를 갖는 지점입니다.

### 사용 초기 → 장시간 사용 시

| 사용 초기 | 장시간 사용 시 |
|---|---|
| 컴퓨터와 일정 거리 유지 | 상체가 모니터로 기울고 눈이 건조해짐 |
| 좌우 하중이 대칭 | 한쪽으로 기울어 비대칭 하중 |
| 정상적인 깜빡임 | 깜빡임이 크게 감소 |

---

## 2. 주장하는 것 / 주장하지 않는 것

| | |
|---|---|
| **주장한다** | **자각** — 사용자는 얼마나 오래 굳어 있었는지, 깜빡임이 얼마나 줄었는지 모른다 |
| | **움직임 유도** — 정적 부하 완화 |
| | **비침습적 개입** — 작업을 끊지 않고 알린다 |
| **주장하지 않는다** | 통증 예방 / 치료 |
| | 특정 "올바른 자세" 의 강제 |

심사에서 "효과가 있다는 근거가 있는가" 가 나왔을 때 답변 가능 여부가 이 구분에서 갈립니다.

---

## 3. 페르소나

| | 페르소나 1 | 페르소나 2 |
|---|---|---|
| | 7년차 프로그래머, 32세 | FPS 프로게이머 연습생, 19세 |
| 사용 | 하루 10–12시간, 3–4시간 연속 | 하루 8시간 이상 고강도 |
| 증상 | 만성 목·어깨 통증, 손목터널 초기 | 클릭 미스 증가, 안구 건조 |
| 니즈 | 통증이 심해지기 전 **강제로라도 쉬게 하는** 피드백 | 컨디션 저하를 **객관적 데이터로** 확인 |

둘 다 **"몰입 중이라 스스로 못 멈춘다"** 가 핵심입니다.

---

## 4. 무엇을 측정하는가

### 4-1. 의자

| 부위 | 센서 | 무엇을 보나 |
|---|---|---|
| 상체 | 압력 4채널 (FSR406, A0~A3) | 좌우·전후 하중 밸런스 |
| 허리 | 적외선 거리 (VL53L0X) | 허리가 등받이에 붙어 있는가 |

압력 채널 순서는 **[전좌, 전우, 후좌, 후우]** 로 고정입니다. 순서를 바꾸면 판정이 뒤집힙니다.

현재 좌우 balance는 `(FL + BL) - (FR + BR)`의 raw 차이를 사용합니다. 한 사용자와
한 Chair 설치에서 수집한 제한된 실측값은 CENTER `-1..+65`, LEFT `+415..+1562`,
RIGHT `-618..-1045`였으며, 그 관측 공백 안의 provisional engineering calibration으로
편중 진입 `±200`, CENTER 복귀 `±100`을 사용합니다. 이는 의학적 자세 경계나 모든
사용자·Chair에 보편적인 값이 아니며, 더 다양한 체중·착석 위치·센서 포화 조건의 raw
data를 모은 뒤 다시 검증해야 합니다.

### 4-2. 웹캠

| 지표 | 방법 |
|---|---|
| **분당 눈 깜빡임 수** | Face Mesh 눈 윤곽 → EAR (Eye Aspect Ratio) |
| **저깜빡임 지속 시간** | 임계 이하가 유지된 연속 초 |
| **얼굴 ↔ 모니터 거리** | 얼굴 폭 픽셀의 역수. 개인 캘리브레이션으로 보정 |

**외부캠을 씁니다.** 노트북 내장캠은 각도에 예민해 값이 불안정합니다.

**영상은 백엔드로 보내지 않습니다.** 수치만 보냅니다.

Vision은 Chair-only 핵심 경로를 막지 않는 선택적 producer입니다. Backend 연결이
끊겨도 카메라와 수치 처리는 계속하며, 연결 중 생성된 값만 실시간 전송하고 연결이
없는 동안의 payload는 저장하거나 재전송하지 않습니다. 카메라 읽기가 반복 실패하면
capture를 해제하고 backoff를 두어 다시 엽니다. 시연 실행기에서도 Vision만 종료된
경우 Chair·Backend·대시보드는 계속 실행합니다.

ACTIVE measurement에서 Backend는 최신 Vision payload를 session cache에 보관하지만
Vision event 자체로 Fusion을 실행하지 않습니다. Chair event가 authoritative tick이며,
서버 monotonic 수신 나이가 3초 미만이고 Chair/Vision sender `t` 차이도 3초 미만인
Vision만 Chair sample에 병합합니다. stale Vision은 병합하지 않아 Chair-only 경로를
유지합니다. 신규 session start는 빈 cache로 시작하고 stop은 cache를 즉시 비우므로,
OFF 중 도착한 Vision이나 이전 session 값은 다음 session에서 재사용하지 않습니다.

`blink_rate`는 최대 60초 rolling metric입니다. 필드가 없거나 Vision이 stale이면
저깜빡임 연속시간을 리셋하지 않고 freeze합니다. `detect_rate == 0`은 최근 품질창에서
얼굴 관측이 전혀 없다는 뜻이므로, 이때 `blink_rate`가 있더라도 state 판정에는
unavailable로 취급해 같은 방식으로 freeze합니다. 단일 frame의 `face_detected=false`만으로
rolling metric을 무효화하지 않으며, 0보다 큰 임의의 검출률 quality threshold는 실제
webcam E2E 이후 결정합니다. 거리 metric은 기존대로 필드 생략 시 freeze합니다.

Vision startup calibration은 기존 얼굴 폭 기반 거리 보정과 OPEN EAR baseline 측정을
함께 수행합니다. detector 초기화 시간이 측정 창을 소비하지 않도록 첫 유효 샘플에서
3초 창을 시작하고, 얼굴 검출·정면·유한한 좌우/평균 EAR 조건을 만족하는 프레임을
최소 20개 요구합니다. OPEN EAR baseline은 자연스러운 순간 blink의 영향을 줄이기 위해
유효 평균 EAR의 중앙값을 사용하며 `vision/baseline.json`의 `open_ear_baseline`에 저장합니다.
기존 파일에 이 필드가 없거나 calibration이 실패하면 기존 절대 임계 `0.21 / 0.25`를
사용합니다.

개인화 blink 임계는 같은 상태 기계를 유지한 채 `open_ear_baseline`에 engineering ratio를
곱해 주입합니다. v1 후보는 `closed_ratio=0.225`, `open_ratio=0.50`이며, 한 피험자의
조명 OFF/ON guided recording 두 건에서 확인한 넓은 후보 구간 안에서 닫힘과 재개방 사이
hysteresis를 충분히 확보하도록 택한 provisional 설정입니다. 생리학적·의학적 기준이나
보편 임계로 해석하지 않으며 `vision/config.py`에서 조정합니다.

`open_ear_baseline`은 눈을 정상적으로 뜬 모양의 기하학 기준입니다. 최소 10초 관측이
필요한 `blink_rate_baseline`은 깜빡임 빈도 기준으로 서로 다른 개념이며, 3초 startup
calibration으로 해결하지 않습니다. 개인 blink-rate 기반 판정이나 penalty에는 아직
사용하지 않습니다.

#### Vision Phase B.5: 관측용 좌우 이동

Vision은 개인 중립 calibration의 유효 정면 프레임에서 얼굴 경계 landmark 234/454의
중심 X, 얼굴 폭, 바깥 눈꼬리 landmark 33/263의 image-plane roll을 수집하고 각각
중앙값을 baseline으로 저장합니다. 얼굴 중심과 폭은 영상 폭으로 먼저 정규화하며,
실시간 관측값은 다음과 같습니다.

`face_lateral_offset = (current_center_x_ratio - neutral_center_x_ratio) / current_face_width_ratio`

raw 영상 좌표에서 음수는 image-left, 양수는 image-right입니다. OpenCV preview는 현재
flip하지 않지만, 사용자가 보는 다른 webcam 화면은 mirror될 수 있으므로 이 부호를
사용자의 해부학적 LEFT/RIGHT로 보편적으로 해석할 수 없습니다. 현재 설치의 실제 webcam
검증에서는 양수=사용자 LEFT, 음수=사용자 RIGHT mapping이 반복 확인되었습니다.

Phase B.5의 `face_lean_direction`은 이 설치에서 검증한 provisional hysteresis를 사용합니다.
LEFT 진입 `>= +0.20`, LEFT 해제 `<= +0.10`, RIGHT 진입 `<= -0.15`, RIGHT 해제
`>= -0.08`입니다. 한 피험자·한 webcam 설치에서 얻은 engineering 값이며 보편적 또는
의학적 기준이 아닙니다. 다른 mirror/camera 환경은 방향 mapping과 임계를 다시 검증해야
합니다. 관측이 없거나 유효하지 않으면 `UNKNOWN`을 출력하고 마지막 유효 내부 상태를
보존하며, 다시 유효해지면 그 상태에서 hysteresis를 재개합니다.

`head_roll_deg`는 두 바깥 눈꼬리를 image x 순서로 놓은 선의 `atan2(dy, dx)`이며,
image-right로 내려가는(clockwise) 선이 양수입니다. `head_roll_delta_deg`는 개인 중립
roll과의 최단 signed angle 차이입니다. Head roll은 머리만 기울인 경우와 몸통 이동을
구별하지 못하므로 보조 관측값일 뿐입니다.

이 값들은 Chair의 `(FL + BL) - (FR + BR)` 압력 분포인 `balance`와 서로 다른 현상을
측정합니다. Vision lateral 값과 `face_lean_direction`은 state 전이, SOMA Load,
Chair balance, confidence, reasons, Feedback에
사용하지 않으며 의료적 자세 평가도 아닙니다. calibration/face/frontal geometry가
유효하지 않으면 값을 생략하고 CENTER로 대체하지 않습니다. Phase C는 계속 비활성입니다.

### 4-3. 왜 깜빡임인가 — 자세 각도 대신

깜빡임은 **사건(event)** 이고 지표는 **빈도(rate)** 입니다.
카메라 각도·초점거리 근사·개인 얼굴 비율이 빈도에는 영향을 주지 않습니다.

반면 절대 각도(거북목 각도 등)는 정면 단일 카메라에서 조건수가 나쁩니다.
전후 기울기는 이미지 평면에 단축으로만 나타나 랜드마크 노이즈에 묻힙니다.

**변화량·비율을 쓰고 절대값을 쓰지 않는다** — 이 원칙이 지표 선택 전반에 적용됩니다.

---

## 5. 시스템 아키텍처

```
┌── 수집 계층 ──────────┐
│  Chair UNO             │  압력4 + ToF 입력 ─┐
│  웹캠 (외부캠)         │  거리 + 깜빡임  ─┤
└───────────────────────┘                   │  source 별 독립 전송
                                            │  각자 t 를 찍는다
                                            ▼
┌── 서버 및 저장 계층 ──────────────────────────────────┐
│  Flask + Socket.IO (:5000)                            │
│    · t 로 소스 병합                                    │
│    · 스키마 검증                                       │
│    · fusion 호출 → 상태 결정                           │
│    · DB 적재 (큐 + 쓰기 스레드)                        │
│  PostgreSQL (Supabase)                                │
└───────────────────────────────────────────────────────┘
        │  state                      ▲  report
        ▼                             │
┌── 인터페이스 계층 ─────┐   ┌── 피드백 출력 (역방향) ──────┐
│  웹 대시보드           │   │  Feedback Nano ← 서버       │
│  팝업 알림             │   │  LED + 진동 모터             │
└────────────────────────┘   └─────────────────────────────┘
```

### 계층 간에 오가는 것

전부 `docs/contracts/` 에 정의돼 있습니다. **코드보다 계약이 먼저입니다.**

| 계약 | 방향 |
|---|---|
| `sensor_data.schema.json` | 수집 → 서버 |
| `state.schema.json` | fusion → 서버 → UI·피드백 |
| `feedback.schema.json` | **서버 → 액추에이터 (역방향)** |
| `report.schema.json` | DB → 서버 → 프론트 |

### 왜 `t` 와 `source` 가 필수인가

의자와 웹캠은 **서로 다른 프로세스**입니다. 각자 보내고 서버가 `t` 로 병합합니다.
`t` 가 없으면 시간축 정렬이 불가능하고, 정렬이 안 되면 멀티모달 융합이 성립하지 않습니다.
**보내는 쪽이 측정 시점에 찍습니다.** 수신 시각은 전송 지연이 섞여 쓸 수 없습니다.

### 왜 판정을 `fusion/` 으로 뺐는가

이전 구조는 서버(`app.py`) 안에서 상태를 판정했습니다. 두 가지가 문제였습니다.

1. 분석 담당자들이 코드를 넣을 자리가 없어 백엔드 파일 하나를 같이 고쳐야 했습니다
2. 하드웨어와 소켓 없이는 판정 로직을 테스트할 수 없었습니다

`fusion/state.py` 는 **순수 함수**입니다. 시계·소켓·DB에 접근하지 않고 `now` 를 인자로 받습니다.
그래서 녹화 로그를 재생해 *"임계 A 면 하루 알림 N 회"* 같은 표를 뽑을 수 있고,
그 표가 파라미터 결정의 근거이자 발표 자료가 됩니다.

### 인증된 측정 세션

현재 제품은 실제 Chair 1대를 전제로 하며 동시에 하나의 measurement session만
`ACTIVE`가 될 수 있습니다. Backend는 Supabase access token을 검증한 뒤 JWT의
`sub`를 `user_id`로 사용하고, start마다 별도의 `session_id`를 만듭니다. 클라이언트가
보낸 `user_id`와 표시용 `user_name`은 데이터 소유권 판단에 사용하지 않습니다.

측정이 `OFF`이면 `sensor_data`를 계약으로 검증하는 데서 멈춥니다. `ACTIVE`일 때만
검증 → Fusion → 인증 사용자의 Socket.IO room 전송 → 비동기 DB 저장 순서로 처리합니다.
서버 재시작 후에는 session을 복원하지 않고 `OFF`로 시작합니다. `device_id`는 Chair
metadata로 저장하지만 사용자 데이터 격리나 history 소유권 기준으로 사용하지 않습니다.

Measurement는 인증된 사용자 Socket.IO 연결을 runtime lease로 사용합니다. 같은 사용자의
socket이 여러 개면 하나가 남아 있는 동안 ACTIVE를 유지하고, 마지막 owner socket이
끊기면 session cache·Feedback state·persistence checkpoint를 정리해 `OFF`로 전환합니다.
REST stop의 access token이 만료되어 401이 되더라도 이미 handshake에서 인증된 owner
socket의 disconnect가 abandoned session을 남기지 않습니다. Socket.IO token은 handshake
시점에 검증되며 연결 중 JWT 만료를 주기적으로 재검증하지는 않습니다. 따라서 연결이
살아 있는 동안 measurement도 유지되고, 다른 사용자가 이를 넘겨받지 못합니다. 브라우저
refresh나 네트워크 단절은 안전한 방향으로 측정을 종료하므로 재연결 후 새 START가 필요합니다.
기존 ACTIVE owner의 인증 user socket이 하나도 없는 orphan 상태라면, 다음에 START를
요청한 다른 인증 사용자가 오기 전에 기존 session을 먼저 종료하고 새 session을 만듭니다.
Owner socket이 하나라도 살아 있으면 기존대로 `measurement_in_use`이며 소유권을 넘기지
않습니다. 임의의 시간 timeout은 정상 장시간 측정을 자를 근거가 없어 이번 단계에서는
도입하지 않습니다.

`GET /api/state/history`는 Supabase Bearer access token 인증이 필수입니다. Backend는
token의 `sub`와 현재 ACTIVE measurement의 `session_id`를 결합해 `state_logs`를
조회하며, 클라이언트가 보낸 `user_id`, `user_name`, `device_id` 또는 `session_id`로
현재-session의 소유권을 정하지 않습니다. 기본 조회 범위는 최근 5분이고 최대 60분이며,
baseline도 동일한 `user_id + session_id` 안에서만 선택합니다. 과거 session 조회는
향후 별도 API에서 session 소유권을 검증한 뒤 제공할 수 있습니다.

---

## 6. 상태

```
NORMAL → CAUTION → DANGER        (+ ABSENT: 자리 비움)
```

실행 profile은 `.env`의 `SOMA_MODE=demo|normal`로 선택합니다. 환경변수가 없으면
졸업작품 내부 시연을 위한 `demo`가 기본입니다. 두 profile은 같은 Fusion 알고리즘과
센서 임계값을 사용하며, 아래 시간 임계값과 DB periodic snapshot 주기만 다릅니다.

| 판정 근거 | Demo 주의 | Demo 위험 | Normal 주의 | Normal 위험 |
|---|---:|---:|---:|---:|
| `low_blink_sec` 저깜빡임 지속 | 10초 | 20초 | 300초 | 900초 |
| `static_hold_sec` 정적 유지 | 10초 | 20초 | 1200초 | 2700초 |
| `close_distance_sec` 근접 지속 | 10초 | — | 300초 | — |
| `imbalance_sec` 좌우 편중 지속 | 10초 | — | 300초 | — |

DB periodic snapshot 주기는 Demo 5초, Normal 30초입니다. state 변경은 즉시,
동일 state는 이 주기로 bounded queue의 DBWriter에 비동기 저장합니다. 저장 기능은
실시간 상태 계산 경로와 분리하며 DB 장애가 Fusion 또는 UI 전송을 막아서는 안 됩니다.

**Demo 값은 기능 시연을 위해 시간축만 축소한 설정입니다. 생리학적·의학적 기준이나
실사용 권고 시간으로 해석하거나 표현하지 않습니다.** Normal 값도 실측 데이터로
재조정하기 전까지 확정값으로 취급하지 않습니다.

**히스테리시스**: 승격 임계와 강등 임계를 다르게 둡니다 (예: 깜빡임 8 미만 승격 / 11 이상 회복).
단일 임계는 경계에서 반드시 채터링을 만들고, 표시가 초당 몇 번 깜빡이면 사용자는 그 자리에서 끕니다.

### SOMA Load Model v1

`score`는 `100 - (static + balance + blink + distance penalty)`를 0~100으로
제한하고 half-up 방식으로 정수화한 **관측 부하 인지용 점수**입니다. 상태 전이와
독립적이며 의료·질병 위험·자세 정답 점수가 아닙니다. 현재 Chair 단계에서는 static과
balance만 계산하고, Vision 입력 생명주기가 정의되기 전까지 blink와 distance penalty는
0으로 둡니다. missing sensor를 정상으로 추정하는 것이 아니라 측정할 수 없는 항목을
임의 감점하지 않는 정책입니다. penalty 내부값은 state 계약에 추가하지 않습니다.

각 penalty는 현재 연속시간 곡선의 양의 변화량만 잔여값에 더합니다. 움직임 또는 CENTER
복귀 시 선형 회복하고, 반복 episode의 잔여 부하는 의도적으로 누적합니다. profile은
알고리즘을 나누지 않고 곡선 시간축과 회복시간만 선택합니다.

| 항목 | Demo | Normal |
|---|---|---|
| static 곡선 `(초, penalty)` | `(0,0) (5,0) (10,5) (20,10) (30,15) (45,20) (60,25)` | `(0,0) (300,0) (600,5) (1200,10) (1800,15) (2700,20) (3600,25)` |
| static 완전 회복시간 | 10초 | 600초 |
| balance 곡선 `(초, penalty)` | `(0,0) (2,0) (5,5) (10,10) (20,15) (30,20) (45,25)` | `(0,0) (5,0) (15,5) (30,10) (60,15) (120,20) (180,25)` |
| balance 완전 회복시간 | 6초 | 180초 |

Demo balance 값 역시 졸업작품에서 누적과 회복을 짧게 확인하기 위한 시간축 축소값이며
생리학적 기준이 아닙니다. 모든 parameter는 향후 사용자 실험 결과에 따라 조정할 수
있는 engineering 설정입니다. 샘플 간격이 profile의 `max_gap_sec`를 넘거나 시간이 역행하면
상태 누적과 마찬가지로 load의 증가·회복도 그 구간에서는 진행하지 않습니다.

**`ABSENT` 는 불량이 아닙니다.** 자리 비움 시 상태 판정용 연속 누적값은 전부
리셋하지만, SOMA Load의 잔여 penalty는 보존하며 회복시킵니다. 자리 비움 60초 미만은
1배, 60초 이상 180초 미만은 1.5배, 180초 이상은 2배 회복속도를 적용하고 경계를
가로지른 시간은 구간별로 정확히 나눕니다.

---

## 7. 피드백

### 7-1. 세 채널과 각자의 자리

| 채널 | 위치 | 왜 거기인가 |
|---|---|---|
| **모니터 상단 LED** | 스크린 탑 쉘프 | 시각은 **시선 주변부**에 있어야 도달. 의자는 사용자의 뒤·아래라 시야 밖 |
| **의자 진동** | 의자 (부착 위치 미정) | 햅틱은 **몸에 닿아야** 함 |
| **화면 팝업 / 대시보드** | 노트북 | 숫자·표는 **사용자가 볼 준비가 된 때** 본다 |

### 7-2. 배선 — 센서 UNO와 Feedback Nano는 분리합니다

노트북이 허브입니다. Chair UNO와 Feedback Nano는 **서로 다른 COM device**입니다.
Chair UNO는 압력 센서 4개와 ToF를 읽는 input producer이고, Feedback Nano는 LED와
진동 모터만 구동하는 output device입니다.

```
[Chair UNO] ------USB------ [노트북 Backend] ------USB------ [Feedback Nano]
 압력4 + ToF                 Fusion / Policy                    LED + 진동
 sensor_data producer       session / routing                  physical pattern
```

Fusion은 관측 상태를, SOMA Load는 누적 부하 점수를 계산합니다. Feedback Policy는
그 결과로 사용자에게 줄 logical feedback을 결정하고, Server는 measurement session과
사용자·장치 routing을 담당합니다. Nano는 semantic command를 실제 LED/진동 pattern으로
표현할 뿐 Fusion state, score, BREAK 조건을 계산하지 않습니다. Front는 인증된 logical
feedback event를 UI와 popup으로 표현합니다.

Nano 연결이나 출력 실패는 선택 출력 계층의 장애입니다. Fusion, state/score emit,
DB 저장, Front 실시간 경로를 중단시키면 안 됩니다. 기존 `feedback/ambient_led/driver.py`와
Chair UNO 진동 경로는 이전 구현으로만 보존합니다. `tools/demo/run_all.py`의
production/default 경로에서는 실행하지 않으며, Chair bridge의 이전 진동 수신도
`--legacy-vibration`을 명시한 경우에만 활성화합니다.

### 7-3. Feedback Nano LED — 인형은 껍데기, LED가 신호

```
    [ 인 형 ]      ← 껍데기. 정체성·시선 유도. 신호를 지지 않는다
  ━━━━━━━━━━━     ← LED 바 = 받침. 여기가 신호 채널
 [스크린 탑 쉘프]
  [  모 니 터  ]
```

> **주변시는 형태 분해능이 낮고 밝기·위치·움직임에 민감합니다.**

그래서 출력에 **실루엣·아이콘·텍스트를 넣지 않습니다.** 읽어야 하는 정보는 Front가
담고, Nano LED는 `NORMAL / NOTICE / WARNING / BREAK` semantic level을 주변시로
표현합니다. 구체적인 색·밝기·점멸 pattern은 F3 hardware E2E 전에는 확정하지 않습니다.
기존 `pos / width / sat` ambient encoding과 driver는 이전 구현의 console/mock 자산으로
보존하며, Nano cutover 때 유지할지 대체할지 결정합니다.

인형을 반투명으로 사용하는 경우에도 별도 판정 로직을 넣지 않고 받침 LED의 출력만
확산합니다. Nano는 Backend가 보낸 level을 표현할 뿐 센서값을 해석하지 않습니다.

### 7-4. 진동과 LED 의 분업

진동은 1비트라 **무엇 때문인지** 전달할 수 없습니다.
진동이 주의를 부르고, **바가 내용을 담습니다.**

진동 패턴을 늘려 의미를 싣지 않습니다. 패턴 학습을 요구하는 설계는 습관화 전에 버려집니다.

### 7-5. Logical Feedback와 Break Recommendation

Fusion state(`NORMAL / CAUTION / DANGER / ABSENT`)와 Feedback level
(`NORMAL / NOTICE / WARNING / BREAK`)은 별개입니다. 기본 mapping은 NORMAL→NORMAL,
CAUTION→NOTICE, DANGER→WARNING입니다. ABSENT의 물리적 OFF/RESTING 표현은 향후 Nano
adapter 책임이며 logical level에 OFF를 추가하지 않습니다. BREAK는 Feedback Policy가
low score 지속, DANGER 지속, 연속 작업시간 중 하나로 승격시키며 Fusion state에는
추가하지 않습니다. 여러 trigger가 동시에 성립하면 `SUSTAINED_DANGER`, `LOW_SCORE`,
`CONTINUOUS_WORK` 순서로 reason을 선택합니다.

Policy는 measurement session마다 새 immutable state로 시작합니다. `session_sec` 대신
별도 `work_period_sec`를 착석 중에만 누적하고, ABSENT 중에는 일시 정지합니다. 짧은
자리 비움은 기존 작업시간과 BREAK latch를 유지합니다. effective break에 도달하면
BREAK를 해제하고 작업시간·trigger timer를 리셋하며 cooldown을 시작합니다. BREAK가
발생하지 않았더라도 앞선 작업 뒤 effective break가 확인되면 작업시간을 리셋합니다.
동일 level 유지 중에는 `transition=false`이므로 향후 popup/진동을 반복하지 않습니다.

Normal의 초기 engineering parameter는 score 60 이하 120초, DANGER 60초, 연속 작업
3000초, effective break 180초, 권장 휴식 300초, cooldown 900초입니다. Demo는 동일한
정책의 시간축만 각각 10초, 10초, 60초, 10초, 20초, 30초로 줄입니다. 이 값들은
의학적·생리학적 기준이 아니며 실제 사용 및 hardware E2E 결과로 조정합니다.

Backend F2 경로는 유효한 Chair tick마다 `Fusion → state 계약 검증 → user room state
emit → Feedback Coordinator → Feedback Policy → feedback_decision 계약 검증 → 동일 user
room feedback emit → persistence` 순서로 실행합니다. Feedback의 timestamp는 wall clock이
아니라 같은 Fusion tick의 Chair `t`입니다. Vision event는 session cache만 갱신하며 Fusion
또는 Feedback tick을 만들지 않습니다.

현재 feedback decision은 level 변화 때뿐 아니라 매 유효 Chair tick마다 발행합니다. 따라서
재연결한 consumer도 현재 level을 다시 받을 수 있고, popup과 단발성 개입은 `level=BREAK`
이면서 `transition=true`일 때만 실행해야 합니다. Feedback Policy·계약 검증·feedback emit의
실패는 이미 발행된 state나 이후 persistence를 막지 않습니다. Session START는 새 policy
state를 만들고 duplicate START는 유지하며, STOP은 즉시 폐기합니다. Backend 재시작 뒤
policy state 복구는 아직 하지 않습니다.

F3에서 logical feedback은 user room의 `feedback`과 별도로 인증된
`feedback_devices` room의 `feedback_device` event에도 전달됩니다. Nano bridge는 사용자
access token이 아니라 sensor/device shared token과 `feedback_device` role로 연결하며,
sensor producer 권한과 room을 구분합니다. Session STOP과 새 START의 출력 초기화는
`feedback_device_off` event로 전달합니다. 전체 broadcast는 사용하지 않습니다.

Bridge는 logical decision을 다음 serial protocol로 변환합니다.

- `LEVEL,NORMAL|NOTICE|WARNING|BREAK|OFF`: stateful, 중복 허용, idempotent
- `ALERT,WARNING|BREAK`: transition-only 진동 trigger, at-most-once 우선

logical `NORMAL`의 reason이 `ABSENT`이면 물리 출력은 `LEVEL,OFF`입니다. Bridge는 마지막
LEVEL만 캐시하고 Nano의 `READY` 또는 serial 재연결 뒤 LEVEL만 재동기화합니다. ALERT는
큐에 저장하거나 재연결 후 replay하지 않습니다. Backend 재연결 때도 server가 현재 logical
decision 또는 OFF를 device 전용 socket에 다시 보내므로 현재 LEVEL을 복원할 수 있습니다.

Nano bridge는 별도 optional process이며 serial/Socket.IO 연결 실패로 종료되지 않고
backoff 후 재시도합니다. `tools/demo/run_all.py --nano`로 명시적으로 포함할 수 있고 Nano
process가 종료되어도 core demo는 계속됩니다. Firmware는 `millis()` 기반 LED/진동 pattern,
15초 command timeout OFF, 유한 진동을 담당합니다. 2026-10-03 physical validation 기준
Nano 배선은 NeoPixel data D13, LED 6개, vibration input D9입니다. 색·밝기, 점멸 주기와
진동 길이·체감 강도는 별도의 hardware UX 검증 대상으로 유지합니다. 같은 날 production
firmware/bridge와 Backend·Front·mock Chair를 연결한 E2E에서 logical feedback에 따른 Nano
출력 전환 및 measurement STOP 시 물리 출력 OFF를 확인했습니다.

### 7-6. 개입 원칙

1. **정보량 없는 메시지는 무시됩니다.** "자세가 안 좋아요" 는 정보량 0입니다.
   사용자가 모르는 숫자를 줍니다 — "23분째 분당 6회예요"
2. **불량한 순간은 개입하기 가장 나쁜 시점입니다.** 정보량은 0인데 개입 비용은 최대입니다
3. **실시간은 존재를 알리고, 숫자는 리포트가 줍니다.** 진동·LED 는 텍스트를 실을 수 없고,
   실을 수 있게 만들면(소형 디스플레이) 읽기 위해 고개를 돌려야 합니다
4. **판단하지 않습니다.** "나쁜/잘못된/불량" 을 UI 문구에서 쓰지 않습니다. 비교는 어제의 자신과만

---

## 8. 범위

### 포함

의자(압력·적외선) · 웹캠(거리·깜빡임) · 피드백(LED·진동·팝업) · 대시보드 · 보고서

### 제외 — 슬라이드에는 있으나 이번 반복에서 하지 않음

| 항목 | 이유 |
|---|---|
| **키보드/마우스 입력 분석** | 범위 밖 결정 |
| **웹캠 자세 판정** (거북목·측만) | 눈 깜빡임 피로도 측정으로 대체 |
| **Unity 3D 아바타** | 자세 판정을 뺐으므로 미러링할 데이터가 없음. 담당 미배정 |

---

## 9. 유사 서비스와의 차이

| | 방식 | 한계 |
|---|---|---|
| RSI Guard | 키보드 타수·마우스 이동으로 사용시간 측정 → 휴식 권고 | **실제로 일어났는지 모름** |
| Lenovo Wellness Mode | AI 가 자세 모니터링, 화면 밝기·스트레칭 제안 | 카메라 상시 |
| Apple | 트랙패드 진동으로 클릭감 제공 | 피로 측정 아님 |
| MOUNTAIN | 키보드 내 디스플레이로 실시간 정보 | 하드웨어 정보 한정 |

**차이**: 타이머는 사용자가 실제로 일어났는지 모릅니다. 압력센서는 압니다.
"알림 무시하고 계속 앉아 있음" 과 "일어나서 3분 걸었음" 을 구분하는 것은
이 하드웨어만 할 수 있습니다.

---

## 10. 보안

슬라이드에 적힌 것을 **실제로 구현합니다.** 이전 구현은 정반대였습니다.

| 항목 | 이전 | 지금 |
|---|---|---|
| DB 자격증명 | **공개 저장소에 평문 슈퍼유저 계정** | `.env` + 앱 전용 롤 |
| Socket 인증 | 없음 (`cors_allowed_origins='*'`) | 연결 시 토큰 확인 |
| 스키마 검증 | 없음 | 계약 스키마로 검증 후 처리 |
| 영상 | — | **저장하지 않음. 수치만 전송** |

> 공개 저장소에 인증 없는 소켓 + 평문 슈퍼유저 자격증명이 있으면,
> **인터넷의 누구나 DB 에 쓸 수 있습니다.** 자격증명이 한 번 올라가면
> 히스토리에서 지워도 이미 크롤링됐다고 보고 값을 교체해야 합니다.

---

## 11. 알려진 제약

| 제약 | 영향 |
|---|---|
| **의자가 1대이고 공용공간에 고정** | 3일 연속 실사용 테스트 불가. 세션 단위로 측정. `tools/mock` 없이는 대부분이 하드웨어를 기다림 |
| **팝업은 브라우저 실행 중에만** | 탭이 백그라운드여도 되지만 브라우저를 완전히 종료하면 안 뜸 |
| **안경** | 렌즈 반사가 EAR 신뢰도를 떨어뜨림. 표본 집단의 착용률이 높음 |
| **진동 지각** | 쿠션·의복을 통과해 실제로 느껴지는지 미검증. 강하게 올리면 소리가 나 공용공간에서 못 씀 |
| **LED 배치** | 사용자가 자기 노트북만 보면 모니터 위는 시야 밖. 부품 주문 전 5분 배치 테스트 필요 |
| **CDN 의존** | 대시보드가 CDN 4종에 의존. 발표장 네트워크가 막히면 화면이 통째로 깨짐. 로컬 번들 필요 |

---

## 12. 문서 지도

| 문서 | 내용 |
|---|---|
| `docs/architecture.md` | **이 문서.** 기획·아키텍처·범위 |
| `docs/contracts/` | 계층 간 계약. 코드보다 먼저 |
| `docs/decisions/` | 개별 결정 기록 (왜 그렇게 했는지) |
| `docs/reference/ui-prototype/` | 이전 대시보드 시안. 참고용, 실행 불가 |
| `CONVENTIONS.md` | 브랜치·커밋·PR·코드 스타일 |
| `README.md` | 설치·실행 |

---

*이 문서와 슬라이드가 다르면 이 문서가 맞습니다. 슬라이드를 갱신하세요.*
*파라미터 기본값은 근거 없는 추정치입니다. 실측 전까지 확정값으로 취급하지 않습니다.*
