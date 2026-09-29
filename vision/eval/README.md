# 웹캠 정확도 분석

회의 항목 1 — *"눈깜박임 수 측정 및 **정확도 분석**"* 의 후반부입니다.

**측정하는 코드가 있다는 것과 그 값이 맞다는 것은 다릅니다.**
`vision/blink/ear.py` 의 임계값(`EAR_CLOSED=0.21`, `EAR_OPEN=0.25`)과
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
근거가 아닙니다. production `BlinkCounter`와 `60~500ms` duration 조건은 바꾸지 않습니다.

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
