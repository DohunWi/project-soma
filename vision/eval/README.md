# 웹캠 정확도 분석

회의 항목 1 — *"눈깜박임 수 측정 및 **정확도 분석**"* 의 후반부입니다.

**측정하는 코드가 있다는 것과 그 값이 맞다는 것은 다릅니다.**
개인 OPEN EAR calibration이 없을 때 `vision/blink/ear.py`가 사용하는 fallback 임계값
(`EAR_CLOSED=0.21`, `EAR_OPEN=0.25`)과
`vision/geometry.py` 의 `YAW_MAX` 는 지금 **문헌에서 흔히 쓰는 값일 뿐**,
이 카메라·이 피험자에 맞춘 값이 아닙니다. 여기 도구로 실측 기반으로 바꿉니다.

---

## 1. 깜빡임 정확도

### 녹화

```bash
python vision/eval/record.py --guided --subject S01 --condition light-off
python vision/eval/record.py --guided --subject S01 --condition light-on
```

Guided recording은 다음 순서로 자동 진행됩니다.

```text
normal OPEN → 자연스러운 BLINK 10회 → CLOSED_HOLD → recovery OPEN
```

화면이 초록으로 바뀌면 자연스럽게 한 번 깜빡이고, `KEEP EYES CLOSED`에서는
안내가 끝날 때까지 눈을 감습니다. 각 frame에는 guided prompt 기준 `OPEN`,
`BLINK`, `CLOSED_HOLD` label이 기록됩니다. 이 label은 detector의 예측값이 아니라
사용자에게 제시한 동작 구간입니다. BLINK cue 시각도 별도 row로 남습니다.

결과는 다음 경로의 JSONL 파일입니다.

```text
vision/eval/data/<subject>_<condition>_<UTC timestamp>.jsonl
```

각 frame row는 epoch `t`, UTC `timestamp`, `left_ear`, `right_ear`,
`combined_ear`, 호환용 `ear`, `label`, `face_detected`, `frontal`,
`face_width_px`를 포함합니다. 얼굴이 검출되지 않으면 EAR 값은 `null`입니다.
조명 OFF/ON은 반드시 `--condition`을 다르게 지정해 별도 파일로 수집하세요.

EAR 시계열을 그대로 남기고 **검출은 하지 않습니다.**
임계를 바꿔가며 오프라인으로 재평가하려면 원본이 있어야 합니다.

관찰자가 옆에 있다면 `--guided` 없이 스페이스바 방식도 됩니다.
자연스러운 깜빡임을 잡지만 사람 반응 지연이 섞입니다.

### 스윕

```bash
python vision/eval/sweep.py vision/eval/data/S01_light-off_*.jsonl
python vision/eval/sweep.py "vision/eval/data/S01_light-*.jsonl" --top 5
```

출력에는 조건별 left/right/combined EAR 분포, 얼굴 검출·정면 프레임 비율,
OPEN baseline 통계, 기존 absolute threshold(`0.21 / 0.25`) 성능과 relative
threshold sweep이 포함됩니다. 두 파일을 함께 넘기면 각 조건에서 구한 baseline을
같은 조건과 반대 조명 조건에 적용해 cross-condition 결과도 냅니다.

guided frame label은 실제 눈 상태가 아니라 화면 prompt입니다. 도구는 `label == BLINK`
frame을 곧바로 정답으로 쓰지 않고, 별도 cue timestamp와 검출 event를 1:1로
매칭합니다. 기본 window는 사람의 반응 지연을 고려한 `cue - 0.10s`부터
`cue + 1.20s`까지이며 CLI에서 바꿀 수 있습니다.

```bash
python vision/eval/sweep.py "vision/eval/data/S01_light-*.jsonl" \
  --match-early-sec 0.10 --match-late-sec 1.20 \
  --transition-margin-sec 0.25
```

OPEN baseline은 cue 반응 window와 label 전환 전후 margin을 제외한 유효·정면
OPEN frame에서 계산합니다. CLOSED_HOLD 뒤의 OPEN prompt는 recovery 검증 구간이므로
baseline에서 분리합니다. `median`, `p75`, 10% `trimmed_mean`을 나란히 비교하며,
ratio grid 범위와 간격도 CLI 옵션으로 조정할 수 있습니다. CLOSED_HOLD 중 잘못 센
event와 recording 끝까지 `CLOSED_CANDIDATE`가 풀리지 않은 경우를 별도로 표시합니다.
`candidate_stuck_at_end`는 replay의 관측 상태이며, recovery OPEN prompt의 실제 EAR가
낮다면 사용자가 눈을 다시 떴다는 근거가 없으므로 알고리즘 실패로 단정하지 않습니다.

이 도구는 **평가 전용**입니다. 출력의 최고 한 조합을 production에 바로 복사하지
마세요. 한 사용자·두 recording은 후보 범위를 좁히는 자료일 뿐 universal threshold의
근거가 아닙니다. startup calibration v1은 이 제한된 결과에서
`closed_ratio=0.225`, `open_ratio=0.50`을 provisional engineering config로 선택했으며,
피험자 확대 평가 전까지 보편 기준으로 표현하지 않습니다. production `BlinkCounter`의
상태 기계와 `60~500ms` duration 조건은 바꾸지 않습니다.

```bash
python vision/eval/sweep.py --self-test    # 합성 신호로 스윕기 자체 검증
```

### 피험자별로도 돌리세요

전원을 합쳐 스윕하면 한 사람에게 과적합될 수 있습니다.
**한 명씩 따로 돌린 최적값이 서로 크게 다르면** 고정 임계로는 안 되고
개인 캘리브레이션이 필요하다는 뜻입니다.

### 안경

`--glasses` 로 기록해두고 착용/미착용을 나눠 비교하세요.
렌즈 반사와 프레임이 EAR 신뢰도를 떨어뜨리는데, 하필 표본 집단(대학생)의
착용률이 높습니다. **성능 차가 크면 발표에서 한계로 명시해야 합니다.**

---

## 2. 거리 정확도

```bash
python vision/eval/distance_check.py --calib-cm 60 --points 40 50 60 70 80
```

핀홀 근사(`얼굴폭 × 거리 = 상수`)가 실제로 얼마나 맞는지 잽니다.
캘리브레이션에 쓴 거리 하나로 **나머지 거리들이 얼마나 잘 나오는지**가 핵심입니다.

**자로 재세요.** 코 끝에서 카메라 렌즈까지입니다.
캘리브레이션 거리가 틀리면 상수가 통째로 틀립니다.

### 판정 기준

`fusion/state.py` 의 근접 임계는 **45cm** 입니다.
평균 오차가 5cm 를 넘으면 그 임계로 판정하기 어렵습니다 —
그때는 절대 거리 대신 **baseline 대비 변화량**으로 바꾸세요.
"평소보다 13cm 가까워짐" 이 "지금 47cm" 보다 방어하기 쉽습니다.

### 고개 돌림

고개를 옆으로 돌리면 얼굴 폭이 투영상 줄어 **"멀어졌다" 고 오판합니다.**
30도면 폭이 13% 줄어 60cm 가 69cm 로 보입니다.

보정 대신 **버립니다** — cos 로 되살리면 노이즈가 증폭되고, 큰 회전에서는
랜드마크 자체가 부정확해집니다. 거리는 초 단위로 변하는 값이라
프레임을 버려도 문제가 없습니다.

제외 프레임이 너무 많으면 `.env` 의 `VISION_YAW_MAX` 를 올리세요.
도구가 실행 중에 제외 프레임 수를 표시합니다.

---

## 3. 관측용 좌우 이동 기록

Phase B.5의 연속 Vision metric은 다음 guided JSONL로 확인합니다.

```bash
python vision/eval/lean_record.py --subject S01 --condition light-off --recalibrate
python vision/eval/lean_record.py --subject S01 --condition light-on
```

첫 명령은 정면에서 개인 중립 baseline을 다시 잡습니다. 두 번째 명령은 같은 baseline을
재사용하므로 조명 변화에서 offset이 얼마나 이동하는지 비교할 수 있습니다. 저장 위치는
`vision/eval/data/S01_lean_<condition>_<UTC timestamp>.jsonl`입니다.

화면 안내 순서는 다음과 같습니다.

```text
NEUTRAL_CENTER → USER_LEFT_LEAN → NEUTRAL_CENTER → USER_RIGHT_LEAN
→ NEUTRAL_CENTER → HEAD_TILT_ONLY → TORSO_LEAN_HEAD_UPRIGHT
```

각 stage는 시간만으로 자동 시작하지 않습니다. `[PREPARE]` 화면에서 안내된 자세로
이동한 뒤 자세가 안정되면 `SPACE`를 누릅니다. 그때부터 `[RECORDING]` 상태로 기본
3초간 기록하고, 끝나면 다음 stage의 `[PREPARE]`로 이동합니다. `ENTER`도 확인 키로
사용할 수 있으며 `ESC` 또는 `Q`는 파일과 카메라를 정리하고 조기 종료합니다.
PREPARE 중의 이동 frame은 JSONL pose sample로 기록하지 않습니다.

기록된 각 frame은 UTC timestamp, `workflow_state=RECORDING`, guided label,
face detected/frontal/valid 여부,
calibration availability, 정규화 center/width, `face_lateral_offset`, raw/delta roll을
기록합니다. 인증 token이나 영상은 기록하지 않습니다.

중요: offset 음수는 raw **image-left**, 양수는 raw **image-right**입니다. 먼저 자신의
왼쪽으로 움직였을 때 부호를 기록하고, 오른쪽으로 움직였을 때 반대 부호가 나오는지
확인하십시오. 이 검증 전에는 payload 부호를 해부학적 LEFT/RIGHT로 이름 붙이지 않습니다.
이 도구의 guided label은 사용자의 동작 지시이지 production 분류 결과가 아닙니다.

### Lean intensity protocol

CENTER/LEFT/RIGHT 임계를 나중에 실측으로 정하기 위한 mild/medium/large 기록은 전용
protocol을 선택합니다. 기본 7-stage protocol은 바뀌지 않습니다.

```bash
python vision/eval/lean_record.py --subject S01 --condition intensity-light-on \
  --protocol lean-intensity --record-sec 3
```

시퀀스는 다음 9단계입니다.

```text
NEUTRAL_CENTER
→ USER_LEFT_MILD → USER_LEFT_MEDIUM → USER_LEFT_LARGE
→ NEUTRAL_CENTER
→ USER_RIGHT_MILD → USER_RIGHT_MEDIUM → USER_RIGHT_LARGE
→ NEUTRAL_CENTER
```

MILD는 컴퓨터 사용 중 자연스럽게 조금 기대는 정도, MEDIUM은 편안하게 유지할 수
있으면서 좌우 이동이 명확한 정도, LARGE는 기존 방향 검증처럼 두드러진 이동입니다.
센티미터 목표를 두지 않습니다. 모든 단계에서 상체를 옆으로 이동하되 얼굴은 webcam을
향하고 의도적으로 머리를 기울이지 않은 안정 자세를 만든 후 `SPACE`를 누릅니다.
이 label은 평가용 human-guided category이며 production classification threshold가 아닙니다.

### Lean threshold / hysteresis protocol

한 피험자와 한 webcam 설치에서 얻은 다음 provisional engineering 값을 평가합니다.
연속 이동 실험에서 네 전환과 chatter 없음이 확인되어 production의 관측용 classifier도
동일한 `FaceLeanClassifier`를 재사용하지만, 보편적·의학적 기준은 아닙니다.

```text
LEFT entry    offset >= +0.20
LEFT release  offset <= +0.10
RIGHT entry   offset <= -0.15
RIGHT release offset >= -0.08
```

```bash
python vision/eval/lean_record.py --subject S01 --condition threshold-light-on \
  --protocol lean-threshold --record-sec 3 --movement-sec 5
```

시퀀스는 다음과 같습니다.

```text
NEUTRAL_CENTER
→ LEFT_BOUNDARY_OUT → LEFT_BOUNDARY_RETURN
→ NEUTRAL_CENTER
→ RIGHT_BOUNDARY_OUT → RIGHT_BOUNDARY_RETURN
→ NEUTRAL_CENTER
```

모든 stage는 PREPARE에서 `SPACE`/`ENTER`를 기다립니다. NEUTRAL은 안정 자세를 기본
3초 기록하고, boundary stage는 확인 후 기본 5초 동안 천천히 이동하는 transition frame을
전부 기록합니다. JSONL에는 후보 state, 네 임계값, transition 여부와 transition 전후
state가 추가됩니다. 측정 불가 frame은 `UNKNOWN`이며 CENTER로 바꾸지 않습니다.

이 protocol과 candidate JSONL 필드는 평가 도구 안에서만 동작합니다. Production은 방향
관측값만 payload로 전달하며, 의료적·자세 품질 기준으로 해석할 수 없습니다. Fusion state,
SOMA Load, confidence, reasons 및 Feedback에 대한 Vision lean 영향은 계속 비활성입니다.

---

## 보고할 것

| 항목 | 왜 |
|---|---|
| 피험자 수, 안경 착용 비율 | 표본 설명 |
| 피험자별 F1 (합산 아님) | 개인차가 고정 임계로 감당되는지 |
| 안경 착용/미착용 F1 차이 | 알려진 한계의 크기 |
| 거리 평균 절대오차 · 상대오차 | 45cm 임계를 쓸 수 있는지 |
| 얼굴 검출률, 고개 돌림 제외 비율 | 실사용에서 값이 얼마나 자주 비는지 |

`data/` 는 `.gitignore` 대상입니다 — 얼굴에서 파생된 데이터이므로
저장소에 올리지 않습니다. 집계 결과만 문서에 남기세요.
