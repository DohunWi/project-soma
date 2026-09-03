-- 001_align_enums.sql
-- 회의 결정에 맞춰 enum 값을 보충합니다.
--
-- 기존 enum 은 미리캔버스 슬라이드의 피드백 아이디어 기준으로 만들어졌습니다.
-- 그 뒤 회의에서 범위가 바뀌었는데 enum 이 따라가지 않았습니다.
--
--   user_status       ABSENT 없음      → 자리 비움을 기록할 수 없음
--   feedback_method   CHAIR_VIBRATION 없음 → 의자 진동으로 정했는데 MOUSE_VIBRATION 만 있음
--   report_period     SESSION 없음     → 30분 시연 보고서가 세션 단위인데 DAILY/WEEKLY 뿐
--
-- ALTER TYPE ... ADD VALUE 는 트랜잭션 밖에서 실행합니다.
-- 되돌리기 어려우니(값 제거 불가) 실행 전에 한 번 더 확인하세요.
-- 기존 값은 지우지 않습니다 — 이미 쓰인 행이 깨지지 않습니다.
--
--   Supabase 대시보드 → SQL Editor 에 붙여넣기

ALTER TYPE user_status     ADD VALUE IF NOT EXISTS 'ABSENT';
ALTER TYPE feedback_method ADD VALUE IF NOT EXISTS 'CHAIR_VIBRATION';
ALTER TYPE report_period   ADD VALUE IF NOT EXISTS 'SESSION';

-- 범위 밖이 된 값들은 남겨둡니다.
-- 삭제하려면 타입을 새로 만들어 교체해야 하고, 그동안 쓰기가 막힙니다.
-- 코드가 안 쓰면 그만입니다.
--   CURSOR_CHANGE / KEYBOARD_FILTER / MOUSE_VIBRATION  ← 키보드·마우스는 범위 밖
