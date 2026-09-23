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
| `feedback.schema.json` | 서버 → 액추에이터 | `server` → `chair`(진동), `feedback/ambient_led`, `web` |
| `report.schema.json` | DB → 서버 → 프론트 | `db/report` → `server` → `web` |
| `state_history.schema.json` | DB → 서버 → 프론트 | `state_logs` → `server` → `web` |
| `measurement_session.schema.json` | 인증 사용자 ↔ 서버 | `web` → `server` start/stop 응답 |

`state_history`는 Supabase Bearer access token의 `sub`에서 얻은 `user_id`와
Backend가 관리하는 현재 ACTIVE measurement의 `session_id`를 함께 사용합니다.
`user_name`과 `device_id`는 소유권이나 조회 경계가 아닙니다. Front는 Supabase를 직접
조회하지 않고 인증된 Backend의 `GET /api/state/history`만 사용합니다. 현재-session
조회에서는 클라이언트가 `session_id`를 전달하지 않으며, 과거 session 조회는 향후
별도 API에서 동일한 `user_id + session_id` 소유권 검사를 거쳐 확장합니다.

## 왜 `t` 와 `source` 가 필수인가

의자와 웹캠은 **서로 다른 프로세스**입니다. 각자 독립적으로 보내고, 서버가 `t` 로 병합합니다.

`t` 가 없으면 시간축 정렬이 불가능하고, 정렬이 안 되면 멀티모달 퓨전이 성립하지 않습니다.
**보내는 쪽이 측정 시점에 찍습니다.** 서버 수신 시각은 전송 지연이 섞여 쓸 수 없습니다.

## 검증

```bash
python tools/mock/validate.py <샘플.json> <스키마.json>
```

서버는 받은 payload 를 이 스키마로 검증한 뒤 처리합니다. 검증 실패는 조용히 삼키지 말고 로그에 남깁니다.
