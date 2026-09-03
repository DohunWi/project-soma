-- 002_time_and_index.sql
--
-- 1) 측정 시각을 저장할 자리
--
-- sensor_logs.time 의 기본값이 now() AT TIME ZONE 'Asia/Seoul' 입니다.
-- 그러면 DB 수신 시각이 들어가고, 수집 계층이 찍은 시각(계약의 t)이 버려집니다.
-- 의자와 웹캠은 서로 다른 프로세스라 각자의 t 로 정렬해야 하는데,
-- 수신 시각으로 덮이면 정렬이 DB 에서 깨집니다.
--
-- 기존 time 컬럼은 그대로 두고(기존 데이터 보존), 측정 시각을 별도 컬럼에 넣습니다.
-- 이후 쿼리는 measured_at 을 우선 쓰고 없으면 time 으로 대체합니다.

ALTER TABLE sensor_logs ADD COLUMN IF NOT EXISTS measured_at TIMESTAMP;
ALTER TABLE sensor_logs ADD COLUMN IF NOT EXISTS source      VARCHAR(16);

UPDATE sensor_logs SET measured_at = "time" WHERE measured_at IS NULL;

-- 2) 시계열 인덱스
-- 지금은 PK 하나뿐이라 "이 사용자의 이 구간" 쿼리가 전체 스캔입니다.
-- 327행일 때는 문제없지만 실사용 로그가 쌓이면 리포트가 느려집니다.

CREATE INDEX IF NOT EXISTS idx_sensor_logs_user_time
    ON sensor_logs (user_name, "time" DESC);
CREATE INDEX IF NOT EXISTS idx_fatigue_logs_user_ts
    ON fatigue_logs (user_id, "timestamp" DESC);

-- 3) 시퀀스 분리
-- test_30m 이 sensor_logs_id_seq 를 함께 씁니다. 복사할 때 딸려온 것으로,
-- 두 테이블이 id 공간을 나눠 쓰고 있습니다. 지금은 무해하지만 사고 원인이 됩니다.
--
--   CREATE SEQUENCE IF NOT EXISTS test_30m_id_seq;
--   SELECT setval('test_30m_id_seq', COALESCE((SELECT max(id) FROM test_30m), 1));
--   ALTER TABLE test_30m ALTER COLUMN id SET DEFAULT nextval('test_30m_id_seq');
--
-- test_30m 은 시연 기록이라 더 쓰지 않을 것이므로 주석으로만 둡니다.

-- 4) RLS
-- users / user_settings / feedback_logs 는 RLS 가 꺼져 있고
-- anon 롤에 SELECT/INSERT/UPDATE/DELETE/TRUNCATE 권한이 있습니다.
-- anon 키는 프론트엔드에 노출되는 것이 정상인 키이므로, 그대로 두면
-- 키를 가진 누구나 이 세 테이블을 읽고 지울 수 있습니다.
--
-- 백엔드는 service_role/postgres 로 붙어 RLS 를 우회하므로 켜도 동작합니다.
--
--   ALTER TABLE users         ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE feedback_logs ENABLE ROW LEVEL SECURITY;
--
-- 다만 프론트가 supabase-js 로 직접 읽는 부분이 있으면 그 쿼리가 막힙니다.
-- 현재 대시보드는 Socket.IO 로만 받으므로 영향이 없을 것으로 보이나,
-- 프론트 담당과 확인한 뒤 켜세요. 그래서 주석으로 둡니다.
