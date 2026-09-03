# DB

**실제 Supabase 스키마가 정본입니다.** 코드가 스키마를 따라갑니다.

## 테이블

| 테이블 | 용도 | 쓰는 쪽 |
|---|---|---|
| `sensor_logs` | 원시 센서 + 간이 판정 | `server/db_writer` |
| `fatigue_logs` | 상태(`status` enum) + `fatigue_score` | `server/db_writer` |
| `feedback_logs` | 개입 기록 + `is_break_taken` | `server/db_writer` |
| `posture_stats_30min` | 30분 집계 | `db/report/generate.py --save` |
| `users` / `user_settings` | 사용자, 민감도·디폴트 센서값 | (미사용) |
| `temp` · `test_30m` | 과거 시연 기록 | 읽기 전용 |

`test_30m` 에 **28분 24초 / 327행** 의 실제 시연 데이터가 있습니다.
회의 항목 2 의 입력이 이미 존재하므로 새로 수집할 필요가 없습니다.

## enum

```
user_status      NORMAL | CAUTION | DANGER | ABSENT
feedback_method  UI_ALERT | AMBIENT_LIGHT | CURSOR_CHANGE |
                 KEYBOARD_FILTER | MOUSE_VIBRATION | CHAIR_VIBRATION
report_period    DAILY | WEEKLY | SESSION
```

`ABSENT` / `CHAIR_VIBRATION` / `SESSION` 은 `migrations/001` 에서 추가했습니다.
기존 enum 이 슬라이드의 피드백 아이디어 기준으로 만들어져 회의 결정과 어긋나 있었습니다.

`CURSOR_CHANGE` / `KEYBOARD_FILTER` / `MOUSE_VIBRATION` 은 **범위 밖**입니다.
값은 남겨두되 코드가 쓰지 않습니다 — 제거하려면 타입 교체가 필요하고 그동안 쓰기가 막힙니다.

**서버의 `DANGER` 는 DB enum 을 따른 것입니다.** 코드에서 `RISK` 라 쓰지 마세요.

## 시각 규약

`time` / `timestamp` 컬럼은 **`timestamp without time zone` + KST 로컬**입니다.
UTC 로 넣으면 9시간 어긋납니다. `db_writer.kst_naive()` 를 쓰세요.

`sensor_logs.measured_at` 은 **수집 계층이 찍은 시각(계약의 `t`)** 입니다.
`time` 은 DB 수신 시각입니다. 의자와 웹캠이 서로 다른 프로세스라
정렬은 `measured_at` 으로 해야 합니다. 리포트 쿼리는
`COALESCE(measured_at, "time")` 을 씁니다.

## 연결 — IPv6 주의

```
db.<ref>.supabase.co       AAAA 만 있습니다. IPv4 레코드가 없습니다
aws-1-ap-northeast-2.pooler.supabase.com:6543   IPv4 로 붙습니다
```

**IPv6 가 안 되는 네트워크에서는 직접 연결이 불가능합니다.**
학교 공용공간 와이파이가 IPv6 를 안 줄 가능성이 높으므로,
`.env` 에는 **풀러 주소**를 넣으세요. 사용자명 형식이 다릅니다.

```
DB_HOST=aws-1-ap-northeast-2.pooler.supabase.com
DB_PORT=6543
DB_USER=postgres.<project-ref>
```

이건 시연 리스크입니다. 발표장에서 직접 주소로 붙게 해두면 DB 가 통째로 죽습니다.

## 보고서

```bash
python db/report/generate.py --table test_30m          # 화면
python db/report/generate.py --table test_30m --post   # 백엔드로 전송
python db/report/generate.py --table test_30m --save   # posture_stats_30min 적재
```

집계는 SQL 이 합니다. 327행이면 파이썬으로도 되지만 실사용 로그가 쌓이면
전부 가져와 처리하는 방식은 감당이 안 됩니다.

## 확인해야 할 것

### 1. RLS

```
RLS 꺼짐:  users · user_settings · feedback_logs
anon 권한: 모든 테이블에 SELECT/INSERT/UPDATE/DELETE/TRUNCATE
RLS 정책:  전부 0개
```

RLS 켜진 테이블은 정책이 0개라 anon 이 차단됩니다 — 우연히 안전합니다.
**꺼진 3개는 anon 키만 있으면 읽고 지울 수 있습니다.** anon 키는 프론트엔드에
노출되는 것이 정상인 키입니다.

`migrations/002` 에 켜는 문장을 주석으로 뒀습니다.
프론트가 supabase-js 로 직접 읽는 부분이 없는지 확인한 뒤 켜세요.

### 2. 압력 센서 동적 범위 — 하드웨어

`test_30m` 327행 기준:

| 채널 | 표준편차 | 범위 |
|---|---|---|
| FL | **200.9** | 0 ~ 1000 |
| FR | 4.0 | 972 ~ 1001 |
| BL | 1.7 | 996 ~ 1010 |
| BR | 1.9 | 998 ~ 1012 |

**FR / BL / BR 세 채널이 사실상 상수입니다.** 1023 만점에 1007 근처로 붙어
있고 편차가 2 이하입니다. 체중이 실리는 순간 동작 범위 끝으로 가서
그 이상 변화를 잡지 못합니다.

결과적으로 **좌우 균형과 움직임 판정이 FL 한 채널로만 결정됩니다.**
리포트의 "정적 유지 91%" 도 세 채널이 안 움직여서 나온 값입니다.

- 분압 저항을 조정해 동작 범위를 중앙으로 옮겨야 합니다
- FL 만 0 까지 떨어지는 이유도 확인이 필요합니다 (접촉 불량 또는 배치 차이)

`generate.py` 가 매 실행 시 이 경고를 출력합니다.
