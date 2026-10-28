# Project SOMA R25 발표용 데이터

## 1. 사용 원칙

- 시간대는 모두 **2026-10-28 KST**입니다.
- `Observer`는 두 30분 raw 창만 의미합니다.
- `DB`는 `state_logs` 최초·마지막 snapshot 경계이며 정확한 Start/Stop 시각이 아닙니다.
- Observer 공백을 연속 센서 관측으로 연결하지 않습니다.
- 현장 관찰과 raw/DB 근거를 구분합니다.

## 2. 측정 시작·종료와 Observer 공백

| 구간 | 시작 | 종료 | 길이 | 출처 | 신뢰 |
|---|---|---|---:|---|---|
| DB snapshot span | 14:19:59.366 | 15:36:22.670 | 76분 23.304초 | PostgreSQL read-only SELECT | 높음 |
| Observer Part 1 | 14:21:30.715 | 14:51:30.520 | 29분 59.805초 | raw JSONL | 높음 |
| Observer 공백 | 14:51:30.520 | 14:59:19.294 | 7분 48.772초 | 두 raw 경계 | 높음 |
| Observer Part 2 | 14:59:19.294 | 15:29:18.093 | 29분 58.802초 | raw JSONL | 높음 |
| Observer 합계 | 두 창 | 두 창 | 59분 58.607초 | raw JSONL | 높음 |

## 3. 시계열 인포그래픽용 핵심 지점

| 시각 | session_sec | Score | State | Static | Balance | Blink | Distance | Reason/사건 | 출처 |
|---|---:|---:|---|---:|---:|---:|---:|---|---|
| 14:19:59.366 | 약 0 | 100 | NORMAL | 초기 | — | — | — | 최초 저장 snapshot | DB |
| 14:21:30.715 | 91.3 | 100 | NORMAL | 0.000 | 0 | 0 | 0 | Observer 시작 | raw |
| 14:29:59.638 | 600.3 | 95 | NORMAL | 4.770 | 0 | 0 | 0 | 상태 reason 없음 | raw |
| 14:39:58.905 | 1,199.5 | 90 | NORMAL | 9.787 | 0 | 0 | 0 | Score 90이나 static level은 아직 0 | raw |
| 14:40:28.015 | 1,228.6 | 90 | CAUTION | 10.005 | 0 | 0 | 0 | `static_hold` 최초 | raw/DB |
| 14:49:59.175 | 1,799.8 | 85 | CAUTION | 14.589 | 0 | 0 | 0 | 누적 부담 지속 | raw |
| 14:51:30.520 | 1,891.1 | 85 | CAUTION | 15.138 | 0 | 0 | 0 | Part 1 종료 | raw |
| 14:51:30–14:59:19 | — | — | — | — | — | — | — | Observer 기록 공백 | 경계 계산 |
| 14:59:19.294 | 2,359.9 | 84 | CAUTION | 15.813 | 0 | 0 | 0 | Part 2 시작 | raw |
| 14:59:59.448 | 2,400.1 | 84 | CAUTION | 15.813 | 0 | 0 | 0 | episode grace 구간 | raw |
| 15:05:22.671 | 2,723.3 | 83 | CAUTION | 17.336 | 0 | 0 | 0 | FORWARD_CHANGE 확정 | raw |
| 15:07:19.109 | 2,839.7 | 83 | CAUTION | 17.336 | 0 | 0 | 0 | NEUTRAL_RETURN 확정 | raw |
| 15:09:59.715 | 3,000.3 | 83 | CAUTION | 17.336 | 0 | 0 | 0 | 연속 작업 3,000초 부근 | raw |
| 15:11:33.068 | 3,093.7 | 83 | CAUTION | 17.336 | 0 | 0 | 0 | FORWARD_CHANGE 확정 | raw |
| 15:14:34.755 | 3,275.4 | 83 | CAUTION | 17.336 | 0 | 0 | 0 | NEUTRAL_RETURN 확정 | raw |
| 15:19:58.982 | 3,599.6 | 83 | CAUTION | 17.343 | 0 | 0 | 0 | 마지막 episode grace 이후 재누적 | raw |
| 15:28:11.845 | 4,092.5 | 80 | DANGER | 20.003 | 0 | 0 | 0 | `static_hold` DANGER | raw/DB |
| 15:29:18.093 | 4,158.7 | 80 | DANGER | 20.371 | 0 | 0 | 0 | Observer 종료 | raw |
| 15:30:12.296 | — | 79 | DANGER | — | — | — | — | 마지막 착석 snapshot | DB |
| 15:30:21.303 | — | 79 | ABSENT | — | — | — | — | ABSENT state_change | DB |
| 15:36:22.670 | — | 100 | ABSENT | — | — | — | — | 마지막 저장 snapshot | DB |

`—`는 해당 자료가 그 필드를 저장하지 않아 추정하지 않았다는 뜻입니다.

## 4. 상태별 관측 시간

Session Report는 0초 초과·60초 이하 snapshot 간격만 현재 snapshot의 상태에 귀속합니다.

| State | 초 | 발표용 표기 | 비율 |
|---|---:|---:|---:|
| NORMAL | 1,228.643 | 20분 28.643초 | 26.81% |
| CAUTION | 2,863.831 | 47분 43.831초 | 62.49% |
| DANGER | 129.463 | 2분 9.463초 | 2.82% |
| ABSENT | 361.367 | 6분 1.367초 | 7.88% |
| 합계 | 4,583.304 | 76분 23.304초 | 100% |

## 5. Sensor 품질 표

| 지표 | 결과 | 출처 | 신뢰 |
|---|---:|---|---|
| Observer samples | 3,587 | raw | 높음 |
| Pressure valid | 3,587/3,587 | raw | 높음 |
| 내부 1.5초 초과 tick gap | 0 | raw | 높음 |
| FSR FL | 743–1,001 | raw | 높음 |
| FSR FR | 975–1,006 | raw | 높음 |
| FSR BL | 571–996 | raw | 높음 |
| FSR BR | 820–977 | raw | 높음 |
| FSR 0 | 전 채널 0건 | raw | 높음 |
| Pressure sum | 3,252–3,948 | raw | 높음 |
| Chair IR | 39–339mm, 중앙값 61mm | raw | 높음 |
| IR valid | 3,587/3,587 | raw | 높음 |
| Vision FRESH | 3,587/3,587 | raw | 높음 |
| Face detected | 98.829% | raw | 높음 |
| Face distance valid | 82.715% | raw | 높음 |
| Face distance | 36.4–282.3cm, 중앙값 60.0cm | raw | 값의 물리 정확도는 부분 검증 |
| Blink rate | 5–70회/분, 중앙값 43 | raw rolling window | 절대 정확도 PARTIAL |
| EAR mode | personalized 100% | raw | 높음 |

## 6. Fusion 핵심 수치

| 항목 | 값 | 해석 |
|---|---:|---|
| Static 최대 | 20.370789 | R25 Score 감소의 유일한 penalty 슬롯 |
| Balance 최대 | 0 | 모든 Observer 표본 CENTER |
| Blink 최대 | 0 | low-blink 300초 grace 미도달 |
| Distance 최대 | 0 | sustained active 미진입, UNKNOWN은 freeze |
| Score formula mismatch | 0/3,587 | 합산 및 반올림 일치 |
| canonical counter | 6 | 4건 raw 직접 기록, 2건은 Observer 공백 중 발생 |
| Static eligible total 종료 | 2,466.742초 | 누적 부담 시간축 |

## 7. History·Session Report 발표 수치

| 항목 | 값 | 주의 |
|---|---:|---|
| Snapshot | 155 | DB 원본 SELECT |
| state_change | 4 | NORMAL/CAUTION/DANGER/ABSENT |
| periodic | 151 | Normal profile 저장 주기 |
| 시간 가중 평균 Score | 87.29 | ABSENT 제외 |
| 최저 Score | 79 | ABSENT 제외 착석 snapshot |
| 마지막 착석 | DANGER / 79 | 15:30:12.296 |
| 마지막 snapshot | ABSENT / 100 | 15:36:22.670 |
| 주요 reason | `static_hold` 2,993.294초 | 유효 착석 interval 기준 |
| BREAK | 현재 저장 데이터로 확인 불가 | state_logs 계약상 미지원 |

## 8. Feedback 및 Nano 근거

| 항목 | 근거 | 신뢰 |
|---|---|---|
| NORMAL→초록, CAUTION→노랑, DANGER→빨강 | Feedback policy/device projection 코드 | 높음(정책) |
| BREAK 최초 파랑 + 진동 2회 | Nano firmware 및 R24 실물 검증 | 높음 |
| R25 후반 LED·진동 정상 | 사용자 현장 관찰 | 현장 관찰 |
| R25 실제 serial command 시각 | 로그 없음 | NOT VERIFIED |
| BREAK 발생 여부 | Session Report 저장 필드 없음 | NOT VERIFIED by DB |

발표에서는 “R25 현장에서 Nano 동작을 확인했다”고 표현하고, “R25 JSONL에 Nano 명령이
기록됐다”고 표현하지 않습니다.

## 9. 권장 그래프

1. **Score/Static penalty 시계열**
   - X축: session_sec 또는 KST
   - Y축 좌측: Score
   - Y축 우측: Static penalty
   - CAUTION 1,228.6초, DANGER 4,092.5초 수직선
   - Observer 공백은 선을 끊거나 음영 처리

2. **상태 지속시간 누적 막대**
   - NORMAL 1,228.643초
   - CAUTION 2,863.831초
   - DANGER 129.463초
   - ABSENT 361.367초

3. **센서 품질 요약**
   - Pressure/IR/Vision FRESH 100%
   - Face distance valid 82.715%
   - 공백 468.772초를 별도 표기

4. **자세 episode 이벤트 타임라인**
   - Observer 기록 4건만 실선 marker
   - 공백 중 2건은 “counter 증가, raw 없음”으로 별도 표시

## 10. 발표 문구 권장안

> 한 사용자·현재 하드웨어 구성에서 약 60분의 Observer 데이터 3,587개와 155개의 DB
> snapshot을 분석했습니다. Chair pressure와 IR은 전 표본 유효했고, cumulative Static
> 부담이 10점과 20점에 도달할 때 CAUTION과 DANGER로 전환됐습니다. 모든 Observer 표본의
> 최종 Score는 네 penalty 슬롯 합산식과 일치했습니다. 이는 졸업작품의 통합 기능을 검증한
> 결과이며 의료적 피로 진단의 보편적 정확도를 의미하지 않습니다.

