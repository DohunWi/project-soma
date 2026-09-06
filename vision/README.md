# vision — 웹캠

얼굴 랜드마크에서 **눈 깜빡임**과 **모니터 거리**를 재서 서버로 보냅니다.
영상은 절대 보내지 않습니다. 수치만 보냅니다.

담당: @DohunWi @sukjuning-ship-it

---

## 파일 지도

| 파일 | 무엇 | 카메라 필요 |
|---|---|---|
| `run.py` | 캡처 루프. 2Hz 전송 + 깜빡임 사건은 즉시 전송 | ○ |
| `landmarks.py` | **mediapipe 를 부르는 유일한 파일.** 랜드마크 → `{i: (x, y)}` | ○ |
| `blink/ear.py` | EAR 계산 + 깜빡임 상태기계 + 분당 빈도 | ✕ |
| `geometry.py` | 얼굴 폭 · 좌우 회전(yaw) · 정면 판정 | ✕ |
| `calibrator.py` | 개인 baseline (얼굴 폭 ↔ 거리) | ✕ |
| `quality.py` | 검출률 · 고개돌림 폐기율 | ✕ |
| `payload.py` | 계약 payload 조립 (순수 함수) | ✕ |
| `eval/` | 정확도 분석 도구 → `eval/README.md` | 일부 |
| `tests/` | `pytest vision/` — **하드웨어 없이 전부 돌아갑니다** | ✕ |

`mediapipe` 를 import 하는 곳이 `landmarks.py` 하나뿐이라, API 가 또 바뀌어도
그 파일만 고치면 됩니다. 나머지 모듈은 `{인덱스: (x_px, y_px)}` dict 만 봅니다.

## 실행

```bash
python vision/landmarks.py            # 모델 파일 미리 받기 (3.7MB, 발표 전 필수)
python vision/run.py                  # 서버로 전송
python vision/run.py --stdout         # 서버 없이 jsonl
python vision/run.py --preview        # 창에 EAR·거리 표시
python vision/run.py --recalibrate --calib-cm 55   # 자로 잰 거리로 다시 잡기
```

```bash
pytest vision/                        # 카메라·mediapipe 없이 돌아갑니다
```

## 계약과의 대응

보내는 것은 `docs/contracts/sensor_data.schema.json` 의 `source="vision"` 입니다.

| 계약 필드 | 만드는 곳 |
|---|---|
| `blink`, `blink_count`, `window_sec`, `blink_duration_ms` | `blink/ear.py` |
| `blink_rate` | `blink/ear.py` — **관측 시간으로 나눕니다.** 10초 미만이면 보내지 않습니다 |
| `face_distance_cm` | `calibrator.py` (핀홀 근사). 정면 프레임에서만 |
| `face_distance_baseline_cm`, `blink_rate_baseline` | `calibrator.py` |
| `detect_rate`, `yaw_dropped_rate` | `quality.py` |
| `calibrating` | `calibrator.py` |

**값이 없으면 키를 생략합니다. `null` 을 보내지 않습니다.**
`null` 은 스키마 위반이라 payload 하나가 통째로 버려집니다 —
거리 하나 때문에 같이 실려 있던 깜빡임 값까지 사라집니다.
조립은 `payload.py` 한 곳에서만 하고, 테스트가 스키마 파일로 직접 검증합니다.

## 설계 규칙

1. **카메라가 필요한 코드와 아닌 코드를 섞지 않습니다.** 순수 모듈은
   `now` 를 인자로 받고 시계·카메라·소켓에 접근하지 않습니다. 그래서 테스트됩니다.
2. **판정하지 않습니다.** 상태·점수는 `fusion/state.py` 가 정합니다.
   여기서는 측정값과 그 값의 품질만 보냅니다.
3. **절대값보다 비율·변화량.** 깜빡임은 눈 안에서의 비율(EAR)이라 카메라
   거리에 영향받지 않습니다. 거리는 개인 baseline 과 함께 보냅니다.
4. **못 믿을 값은 버립니다.** 고개를 돌리면 얼굴 폭이 투영상 줄어 "멀어졌다"
   고 오판합니다. cos 로 되살리지 않고 그 프레임의 거리를 버립니다.

## 알려진 한계

| 한계 | 상태 |
|---|---|
| `EAR_CLOSED=0.21` / `EAR_OPEN=0.25` 가 문헌값 | **실측 필요.** 정면 얼굴 사진에서 EAR 0.204 가 나왔습니다 — 임계 바로 아래입니다 |
| `YAW_MAX=0.22` 가 추정치 | `eval/distance_check.py` 로 제외 프레임 비율을 보며 조정 |
| 거리 45cm 임계 | `eval/distance_check.py` 로 오차 측정. 5cm 넘으면 baseline 대비 변화량으로 전환 |
| 안경 | 렌즈 반사가 EAR 신뢰도를 떨어뜨립니다. 표본 착용률이 높습니다 |
| mediapipe 1.0.x | macOS 에서 첫 추론에 프로세스가 abort 합니다. `<1.0` 으로 고정 |
| 노트북 내장캠 | 각도에 예민합니다. 외부캠을 씁니다 |
