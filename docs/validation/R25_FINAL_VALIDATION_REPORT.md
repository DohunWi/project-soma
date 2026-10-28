# Project SOMA R25 Normal 최종 통합 검증보고서

## 문서 정보

- 검증일: 2026-10-28 (KST)
- Backend: `Back @ a26e1f3b4fe4560af492fe722989c07006ed0892`
- Frontend: `feat/front-backend-integration @ 92b74955aa120c3bc0cfadd7224604fece7fe2fd`
- 실행 조건: `normal / source-mode=all / real`
- 판정 표기: **PASS / PARTIAL / NOT VERIFIED / FAIL**

### 근거 수준

| 수준 | 의미 |
|---|---|
| 높음 | R25 raw Observer JSONL, Supabase PostgreSQL `state_logs` 읽기 전용 조회, 현재 코드 |
| 중간 | Observer summary, 자동 테스트, 이전 검증 문서 |
| 현장 관찰 | 사용자가 실제 장치·화면에서 확인했지만 해당 명령 자체는 로그에 없음 |
| 미확인 | 현재 자료로 직접 입증할 수 없음 |

## 1. 검증 목적 및 범위 — PASS

R25는 Chair FSR 4채널·IR, Vision EAR/Blink·얼굴 거리, Normal Fusion,
Frontend, Feedback Nano, PostgreSQL History/Session Report를 한 측정 세션에서
통합 확인한 engineering validation입니다.

본 보고서는 다음을 주장하지 않습니다.

- 의료적 피로 진단 정확도
- 모든 사용자·의자·카메라에 대한 보편 임계값 타당성
- Observer가 기록하지 않은 7분 48.772초 구간의 연속 센서 파형
- raw 로그에 포함되지 않은 Nano serial 명령의 직접 증명

## 2. 테스트 환경과 시스템 구성 — PASS

| 구성요소 | R25 구성 | 확인 근거 |
|---|---|---|
| Chair | FSR FL/FR/BL/BR + 등받이 IR | Observer raw 3,587개 유효 표본 |
| Vision | Webcam + MediaPipe + 개인화 EAR | 모든 표본 `FRESH`, `personalized`, baseline `0.244899` |
| Backend | 인증 세션, Calibration, Chair tick merge | 모든 Observer 표본 `MEASURING`, 동일 session_id |
| Fusion | Static cumulative-v2, Balance, Blink score-v1, Phase C Distance | raw penalty 및 diagnostic 슬롯 |
| Front | 인증 기반 Start/Stop, realtime state, 종료 Session Report | 사용자 현장 확인 + API/DB 검증 |
| Feedback | Front popup + Nano LED/진동 | 정책 코드 + 사용자 현장 관찰 |
| 기록 | Supabase PostgreSQL `public.state_logs` | read-only SELECT로 155개 snapshot 확인 |

연결 구조는 다음과 같습니다.

```text
Chair Bridge ─┐
              ├─ sensor_data → Backend cache/Calibration → Fusion
Vision ───────┘                                  │
                                                 ├─ state/phase/feedback → Front
                                                 ├─ feedback_device → Nano Bridge
                                                 └─ DBWriter → PostgreSQL state_logs
```

Supabase Auth와 `state_logs` 저장은 서로 다른 연결입니다. Front access token은
Backend 인증에 사용되며, 실제 snapshot은 Backend PostgreSQL app role이 저장합니다.

## 3. 실행 조건 및 측정 방법 — PASS

### Observer 자료

| 파일 | 관측 구간(KST) | 표본 | 관측 길이 |
|---|---:|---:|---:|
| `r25_normal_part1_20261028_142129.jsonl` | 14:21:30.715–14:51:30.520 | 1,794 | 1,799.805초 |
| `r25_normal_part2_20261028_145917.jsonl` | 14:59:19.294–15:29:18.093 | 1,793 | 1,798.802초 |
| 합계 | 두 관측 창 | 3,587 | 3,598.607초(59분 58.607초) |

- 두 파일은 동일 measurement session입니다.
- 표본 간격 중앙값은 1.004초, 각 파일 내부 최댓값은 1.009초입니다.
- 각 파일 내부에 1.5초 초과 누락은 없습니다.
- Observer 공백은 14:51:30.520–14:59:19.294, **468.772초**입니다.
- 이 공백은 측정 세션 중단이 아니라 Observer 기록 공백입니다. DB에는 같은 세션의
  snapshot이 이어집니다.

### 실제 저장 세션

`state_logs` snapshot 경계는 14:19:59.366–15:36:22.670이며 4,583.304초
(76분 23.304초)입니다. 이는 실제 Start/Stop 시각 자체가 아니라 최초·마지막 snapshot
시각입니다.

## 4. R24 사전 통합 검증 요약 — PASS

R24-3에서 다음 8개 항목을 실제 하드웨어로 통과했습니다.

1. Normal 기본 `cumulative-v2 + score-v1`
2. Demo `legacy + legacy`
3. Chair IR + Vision Calibration과 MEASURING 전환
4. Score/State/Reasons 전달
5. ABSENT 및 재착석 lifecycle
6. Stop과 새 측정의 session-scoped 초기화
7. Front와 Backend 표시 동기화
8. Nano LEVEL, WARNING/BREAK 진동, ABSENT/Stop OFF

세부 근거: [`r24-3-normal-hardware.md`](r24-3-normal-hardware.md)

## 5. R25 실측 데이터 품질 분석

### 5.1 Chair FSR — PASS

| 채널 | 전체 최솟값 | 전체 최댓값 | 0 표본 |
|---|---:|---:|---:|
| FL | 743 | 1,001 | 0 |
| FR | 975 | 1,006 | 0 |
| BL | 571 | 996 | 0 |
| BR | 820 | 977 | 0 |

- 유효 pressure: **3,587/3,587 (100%)**
- pressure sum 범위: **3,252–3,948**
- Observer 구간 `seated=false`: **0건**
- Observer 구간 허위 ABSENT: **0건**
- 순간적인 FSR 0: **0건**

네 채널이 높은 ADC 영역에 분포하므로 센서 포화·개인별 하중 일반화는 별도 위험으로
남습니다. 다만 R25에서는 채널 변화가 존재했고 입력 누락이나 false absence는 없었습니다.

### 5.2 Chair IR — PASS

- 유효 상태: **3,587/3,587 (100%)**
- 전체 범위: **39–339mm**, 중앙값 **61mm**
- Part 1 중앙값 63mm, Part 2 중앙값 54mm
- Part 1 최대 225mm, Part 2 최대 339mm
- pressure와 IR은 같은 payload timestamp를 사용하지만 센서별 acquisition timestamp는 없습니다.

### 5.3 Vision freshness 및 얼굴 검출 — PARTIAL

| 항목 | 결과 |
|---|---:|
| Vision `FRESH` | 3,587/3,587 |
| `face_detected=true` | 3,545/3,587 (98.829%) |
| `detect_rate` | 최소 0.46 / 중앙값 1.0 / 최대 1.0 |
| 개인화 EAR mode | 3,587/3,587 |
| OPEN EAR baseline | 0.244899 고정 |

Vision 전송과 freshness는 안정적이었습니다. 단, 얼굴 거리 품질은 별도 제한이 있습니다.

### 5.4 Blink — PASS(전송·정책), PARTIAL(절대 계수 정확도)

- rolling `blink_rate`: 최소 5, 중앙값 43, 최대 70회/분
- `blink=true`가 Chair tick에 포착된 표본: 582개
- 최대 `low_blink_sec`: 132.497초
- Blink penalty 양수 표본: 0개
- threshold mode: 전 표본 `personalized`
- calibration status: 전 표본 `loaded_personalized`

582개는 1Hz Chair merge 시점에 보인 이벤트 표본이지 전체 실제 Blink 수가 아닙니다.
R23의 수동 계수 12회 대 시스템 13회는 개인화 BlinkCounter가 근접했음을 보여주지만,
R25 자체에는 수동 ground truth가 없으므로 rolling rate의 절대 정확도를 재입증하지 않습니다.

### 5.5 얼굴 거리 — PARTIAL

- 유효: **2,967/3,587 (82.715%)**
- 누락/Phase C UNKNOWN: **620/3,587 (17.285%)**
- 유효값 범위: 36.4–282.3cm, 중앙값 60.0cm
- 누락 연속 구간: 201개, 최장 20.077초
- 누락 중 `face_detected=false`: 42개
- 누락 중 `detect_rate=1.0`: 482개
- 누락 중 `yaw_dropped_rate>0`: 613개

거리 누락 표본의 `yaw_dropped_rate` 중앙값은 0.427로, 거리 존재 표본의 0.0보다
높았습니다. yaw gate 연관성을 강하게 지지하지만 `yaw_dropped_rate`는 이동창 지표이므로
각 누락의 단일 원인이 yaw라고 단정하지 않습니다. UNKNOWN에서는 Phase C temporal과 penalty가
동결되어 잘못된 감점을 방지했습니다.

## 6. Fusion Score·State·Reason 시계열 분석 — PASS

### 6.1 Penalty 슬롯

| 슬롯 | R25 Observer 최댓값 | 실제 Score 기여 |
|---|---:|---|
| Static | 20.370789 | 있음 |
| Balance | 0 | 없음 |
| Blink | 0 | 없음 |
| Distance | 0 | 없음 |

모든 3,587개 표본에서 다음 계산과 저장 Score가 일치했습니다.

```text
Score = round(clamp(100 - static - balance - blink - distance, 0, 100))
```

불일치: **0건**입니다.

### 6.2 State는 단순 Score 임계값이 아님

R25의 State 전환은 cumulative-v2 static level에 의해 발생했습니다.

| 전환 | 시각(KST) | session_sec | Score | Static penalty | Reason |
|---|---:|---:|---:|---:|---|
| NORMAL 시작 | 14:19:59.366(DB 최초 snapshot) | 약 0 | 100 | 초기값 | 없음 |
| NORMAL → CAUTION | 14:40:28.009–015 | 1,228.6 | 90 | 10.004508 | `static_hold` |
| CAUTION → DANGER | 15:28:11.840–845 | 4,092.5 | 80 | 20.002739 | `static_hold` |
| DANGER → ABSENT | 15:30:21.303 | Observer 종료 후 | 79 | DB state change | 없음 |

14:39:58에는 Score가 이미 90이었지만 static penalty가 10점 미만이어서 State는 NORMAL이었습니다.
따라서 Score 90 자체를 CAUTION 경계라고 해석하면 안 됩니다.

### 6.3 Static burden 및 자세 변경

- Observer 시작 static penalty: 0점
- Observer 종료 static penalty: 20.370789점
- `static_eligible_total_sec`: 0 → 2,466.742초
- 최종 `posture_episode_sec`: 846.211초
- canonical event counter: 세션 전체 6건
- Observer가 직접 기록한 canonical event: 4건

직접 기록된 이벤트:

| 시각(KST) | 이벤트 | source | 처리 |
|---|---|---|---|
| 15:05:22.671 | `FORWARD_CHANGE` | pressure+IR | episode 1회 reset, 누적 penalty 유지 |
| 15:07:19.109 | `NEUTRAL_RETURN` | pressure+IR | episode 1회 reset, 누적 penalty 유지 |
| 15:11:33.068 | `FORWARD_CHANGE` | pressure+IR | episode 1회 reset, 누적 penalty 유지 |
| 15:14:34.755 | `NEUTRAL_RETURN` | pressure+IR | episode 1회 reset, 누적 penalty 유지 |

Part 2 시작 시 counter가 이미 2였으므로 Observer 공백 중 2건이 확정됐습니다. 마지막 이벤트
ID 2가 `NEUTRAL_RETURN`이었다는 상태는 남아 있지만, 공백 중 전체 파형과 첫 이벤트 상세는
기록되지 않아 복원하지 않습니다.

자세 변경 후 각 episode의 300초 grace 동안 신규 eligible time은 늘지 않았지만 기존 static
penalty가 감소하거나 회복되지는 않았습니다. 이는 채택한 누적형 정책과 일치합니다.

### 6.4 다른 Fusion 요소

- Balance: 모든 표본 `CENTER`, penalty 0
- Blink: low-blink 최대 132.497초로 score-v1 300초 grace 미도달, penalty 0
- Distance: instantaneous evidence는 존재했지만 sustained `active` 표본 0, penalty 0
- Distance temporal 최대 candidate accumulation: 126.478초
- ABSENT: 두 Observer 창에는 없고, DB에서 15:30:21 이후 확인

일시적 이석/재착석 안정화에 해당하는 lifecycle pause 155 tick에서는 Static/Blink 누적이
동결됐습니다. 이 구간은 Score 회복으로 소급되지 않았으며 R5 안정화 정책과 일치합니다.

## 7. Feedback 및 Nano 동작 검증 — PARTIAL

### 코드상 정책

| Fusion/정책 상태 | 논리 Feedback | Nano steady LED |
|---|---|---|
| NORMAL | NORMAL | 초록 |
| CAUTION | NOTICE | 노랑 |
| DANGER | WARNING | 빨강 |
| BREAK latch | BREAK | 최초 파랑 최소 3초 후 현재 Fusion 색상 |
| ABSENT/Stop | NORMAL 또는 OFF projection | OFF |

Nano 진동은 transition-only입니다.

- WARNING transition: 1회
- BREAK transition: 2회
- LEVEL 유지·재연결: 과거 ALERT 재실행 없음

Normal BREAK 조건은 `DANGER` 60초, Score 60 이하 120초, 또는 연속 착석 3,000초 중
하나입니다. R25 세션은 3,000초를 넘었고 DANGER도 129.463초 관측됐으므로 정책상 BREAK
조건을 충족합니다.

### 확인 범위

- **현장 관찰:** R25 후반 DANGER 진입 이후 Nano LED와 진동이 정상 동작했다고 사용자가 확인
- **코드/테스트:** LEVEL/ALERT, 3초 BREAK projection, 재진동 방지 확인
- **로그 미포함:** R25 Observer와 `state_logs`에는 실제 Nano serial 명령·진동 완료 이벤트가 없음
- **Session Report:** BREAK 발생 여부는 의도적으로 `지원하지 않음`으로 반환

따라서 하드웨어 동작은 현장 검증으로 PASS이지만, R25 데이터 파일만으로 serial 명령 시각까지
재구성하는 검증은 **NOT VERIFIED**입니다.

## 8. History·Session Report 검증 — PASS(저장/조회), PARTIAL(정확한 lifecycle 경계)

Supabase PostgreSQL을 read-only SELECT하여 동일 session의 원본을 확인했습니다.

| 항목 | 결과 |
|---|---:|
| snapshot | 155개 |
| state_change / periodic | 4 / 151 |
| snapshot 경계 | 14:19:59.366–15:36:22.670 |
| snapshot span | 4,583.304초 |
| 확인 가능한 착석 시간 | 4,221.937초 |
| ABSENT 관측 시간 | 361.367초 |
| 60초 초과 미확인 공백 | 0초 |
| 시간 가중 평균 Score(ABSENT 제외) | 87.29 |
| 최저 착석 Score | 79 |
| score model | `soma_load_v1` |
| legacy unversioned | false |

상태별 관측 시간:

| 상태 | 시간 |
|---|---:|
| NORMAL | 1,228.643초 (20분 28.643초) |
| CAUTION | 2,863.831초 (47분 43.831초) |
| DANGER | 129.463초 (2분 9.463초) |
| ABSENT | 361.367초 (6분 1.367초) |

- 마지막 착석 snapshot: 15:30:12.296, DANGER, Score 79, `static_hold`
- 마지막 저장 snapshot: 15:36:22.670, ABSENT, Score 100
- Front 종료 세션 History/Session Report 표시: 사용자 현장 확인
- DB owner: 동일 session의 distinct owner 1명(식별값은 출력하지 않음)

세션 status나 정확한 Stop 시각을 영속화하는 별도 lifecycle 테이블은 없습니다. 보고서의 시작·종료는
설계대로 최초·마지막 snapshot 경계이며, 마지막 snapshot 이후 시간을 추정하지 않습니다.

## 9. 발견된 문제, 한계 및 잔여 위험

| 항목 | 판정 | 영향 |
|---|---|---|
| Observer 공백 468.772초 | PARTIAL | 세션은 이어졌지만 해당 구간 raw 파형·두 canonical event 전체를 직접 검증할 수 없음 |
| 얼굴 거리 누락 17.285% | PARTIAL | Phase C가 UNKNOWN/freeze로 안전 처리하나 거리 감지 가용성 감소 |
| 얼굴 거리 282.3cm 최대값 | PARTIAL | 유효성 gate를 통과한 outlier 가능성; 단일 세션에서 정확도 일반화 불가 |
| FSR 높은 ADC 영역 | PARTIAL | R25 안정성은 확인됐으나 포화 환경에서 자세 구분 감도 일반화 불가 |
| Balance penalty 0 | NOT VERIFIED | R25는 CENTER 위주여서 좌우 penalty 활성화 자체를 재검증하지 않음; 이전 실험 근거는 존재 |
| Blink rolling rate 절대 정확도 | PARTIAL | R23 수동 비교는 양호하나 R25 ground truth 없음 |
| Nano serial command 원본 | NOT VERIFIED | 현장 관찰 및 정책 테스트만 존재 |
| 의학적 타당성 | NOT VERIFIED | engineering prototype 범위 밖 |

확인된 P0/P1 production 결함은 없습니다. 얼굴 거리 누락과 FSR 포화 가능성은 현재 안전 처리와
한계 문서화가 필요한 품질 이슈이며, 이번 결과만으로 threshold를 변경할 근거는 아닙니다.

## 10. 최종 합격 판정과 추가 검증 필요 여부 — PASS

### 결론

Project SOMA는 졸업프로젝트 핵심 기능에 대해 다음 통합 검증을 완료했다고 판단합니다.

- 실제 Chair·Vision 입력 수집과 session Calibration
- Normal cumulative-v2 Static 누적 및 score-v1 Blink 보호
- penalty 합산, State/Reason 전환, ABSENT 처리
- Front realtime 표시와 종료 History/Session Report
- Feedback Nano LED·진동의 실제 장치 동작
- PostgreSQL `state_logs` 저장과 인증 사용자별 조회

이는 **한 사용자·한 장비에서의 기능적/공학적 합격**이며 의료적·통계적 유효성 합격은 아닙니다.

현재 정책·하드웨어 연결을 변경하지 않는다면 추가 실측 없이 집에서 발표자료와 GUI 실행기 개발을
진행해도 됩니다. 다음 경우에만 재실측이 필요합니다.

- FSR 회로나 센서 위치 변경
- yaw/EAR/Phase C 임계값 변경
- Fusion penalty 또는 Feedback policy 변경
- GUI 실행기가 기존 Start/Calibration/Stop 순서를 변경

## 11. 졸업 발표용 핵심 수치 및 그래프 데이터

발표에는 다음 수치를 사용하는 것이 안전합니다.

- Observer: 2개 창, **3,587개**, **59분 58.607초**, pressure 유효 100%
- Observer 내부 Chair tick 누락: **0건**
- Observer 공백: **7분 48.772초**
- FSR 0값: **0건**
- IR 유효: **100%**, 39–339mm
- Vision freshness: **100%**
- 얼굴 검출: **98.829%**
- 얼굴 거리 유효: **82.715%**
- Score: Observer 100 → 80, 마지막 착석 DB 79
- CAUTION: 14:40:28, Static 10.004508
- DANGER: 15:28:11, Static 20.002739
- DB snapshot: 155개, 평균 87.29, 최저 착석 79
- 저장 상태 시간: NORMAL 20:28.643 / CAUTION 47:43.831 / DANGER 2:09.463 / ABSENT 6:01.367

그래프용 세부 시간축과 근거 수준은
[`R25_PRESENTATION_DATA.md`](R25_PRESENTATION_DATA.md)에 분리했습니다.

