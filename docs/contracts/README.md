# 계층 간 계약

**여기 있는 파일이 계층 사이를 오가는 데이터의 유일한 정의입니다.**

## 규칙

1. **코드보다 계약이 먼저입니다.** 필드를 추가하고 싶으면 여기부터 고칩니다.
2. **계약 변경은 전원 리뷰입니다.** 나머지 폴더는 담당자 + 1명이면 됩니다.
3. **`v` 를 올리지 않고 필드를 없애거나 의미를 바꾸지 않습니다.** 추가는 자유입니다.
4. 받는 쪽은 **모르는 필드를 무시**합니다. 그래야 한쪽만 먼저 배포해도 안 깨집니다.
5. **값이 없으면 키를 생략합니다. `null` 을 보내지 않습니다.**
   `null` 은 타입 위반이라 payload 하나가 통째로 버려집니다. 같이 실려 있던
   멀쩡한 값까지 사라지므로, 없는 값은 키 자체를 빼는 편이 안전합니다.

## 파일

| 파일 | 방향 | 누가 보내고 누가 받나 |
|---|---|---|
| `sensor_data.schema.json` | 수집 → 서버 | `chair/bridge`, `vision/` → `server` |
| `state.schema.json` | 분석 → 서버 → UI | `fusion` → `server` → `web`, `feedback` |
| `feedback.schema.json` | legacy 출력 계약 | 이전 Chair 진동·ambient LED 자산에서만 사용 |
| `feedback_decision.schema.json` | 정책 → UI·출력 어댑터 | Backend integrated policy → 인증된 `web`, Feedback Nano adapter |
| `report.schema.json` | DB → 서버 → 프론트 | `db/report` → `server` → `web` |
| `state_history.schema.json` | DB → 서버 → 프론트 | `state_logs` → `server` → `web` |
| `history_sessions.schema.json` | DB → 서버 → 프론트 | 인증 사용자의 저장된 session 목록 |
| `measurement_session.schema.json` | 인증 사용자 ↔ 서버 | `web` → `server` start/stop 응답 |
| `measurement_phase.schema.json` | 서버 → 인증 사용자 room | Backend measurement calibration lifecycle → `web` |

`feedback_decision`의 `NORMAL / NOTICE / WARNING / BREAK`는 Fusion state와 별개입니다.
`BREAK`를 `state.schema.json`에 추가하지 않으며, 사용자 소유권은 payload의 `user_id`가
아니라 Backend가 검증한 measurement session과 Socket.IO room으로 정합니다. 선택값이
없으면 다른 계약과 마찬가지로 `null` 대신 키를 생략합니다.

`state_history`는 Supabase Bearer access token의 `sub`에서 얻은 `user_id`와
Backend가 관리하는 현재 ACTIVE measurement의 `session_id`를 함께 사용합니다.
`user_name`과 `device_id`는 소유권이나 조회 경계가 아닙니다. Front는 Supabase를 직접
조회하지 않고 인증된 Backend의 History API만 사용합니다. `GET /api/state/history`는
현재 ACTIVE session 전용이며 기본 최근 5분, 최대 60분, ACTIVE 없음은 409입니다.
`GET /api/state/history/sessions`는 저장된 session 목록을 마지막 snapshot 최신순으로
반환합니다. `first_measured_at`/`last_measured_at`은 실제 Start/Stop 시각이 아닙니다.
NULL session_id인 legacy row는 제외하며 빈 목록은 200입니다.
`GET /api/state/history/sessions/<session_id>`는 UUID 검증 후 DB의 `user_id + session_id`로
소유권을 확인합니다. 타인 소유와 없는 session은 같은 404, 잘못된 UUID/query/시간은 400,
DB 장애는 503입니다. 기본 구간은 마지막 snapshot 직전 5분이며 start/end는 함께 전달하고
최대 60분입니다. 응답은 기존 state_history 계약을 재사용하고 baseline도 동일 소유권 범위입니다.
목록 API는 query를 받지 않으며 상세 API는 start/end만 허용합니다. 요청 user_id는 받지 않습니다.
두 저장 기록 API는 ACTIVE registry와 독립적이어서 Stop/Backend 재시작 후에도 조회할 수 있습니다.

`measurement_phase`의 `OFF / CALIBRATING / READY / MEASURING`은 Backend가 관리하는
측정 실행 단계입니다. Fusion의 `NORMAL / CAUTION / DANGER / ABSENT`와 다른 축이며,
READY는 baseline 수락을 알린 직후 MEASURING으로 진행하는 일회성 전이 이벤트입니다.
명시적 Chair-only source mode에서는 calibration의 Vision 표본이 0/0이고
readiness.vision=true는 Vision 검사를 요구하지 않는다는 뜻입니다. 진행률은 시간과
Chair IR 조건으로 계산합니다. All-source의 Vision 5/8개와 안정성 조건은 유지합니다.
내부 얼굴 baseline의 None은 public payload에 null로 추가하지 않습니다.

## 왜 `t` 와 `source` 가 필수인가

의자와 웹캠은 **서로 다른 프로세스**입니다. 각자 독립적으로 보내고, 서버가 `t` 로 병합합니다.

`t` 가 없으면 시간축 정렬이 불가능하고, 정렬이 안 되면 멀티모달 퓨전이 성립하지 않습니다.
**보내는 쪽이 측정 시점에 찍습니다.** 서버 수신 시각은 전송 지연이 섞여 쓸 수 없습니다.

## 검증

```bash
python tools/mock/validate.py <샘플.json> <스키마.json>
```

서버는 받은 payload 를 이 스키마로 검증한 뒤 처리합니다. 검증 실패는 조용히 삼키지 말고 로그에 남깁니다.
