-- Project Soma — DB 스키마
--
-- 이전 스키마는 raw_data 를 JSON 통짜로 넣고 5초에 한 행만 저장했습니다.
-- 깜빡임은 초 단위 사건이라 그 해상도에서 사라지고, JSON blob 안에 들어가면
-- "t1~t2 구간 깜빡임" 을 쿼리하려면 모든 행을 역직렬화해야 합니다.
-- 30분 시연 데이터로 보고서를 만들 수 없는 구조였습니다.
--
-- 그래서 두 가지를 바꿨습니다.
--   1. 자주 쓰는 값은 수치 컬럼으로 승격 (blob 안에서 꺼냅니다)
--   2. 깜빡임은 이벤트 테이블로 분리

CREATE TABLE IF NOT EXISTS sensor_logs (
    id               BIGSERIAL PRIMARY KEY,
    t                TIMESTAMPTZ  NOT NULL,
    user_name        TEXT         NOT NULL,

    state            TEXT,              -- NORMAL / CAUTION / RISK / ABSENT
    score            SMALLINT,
    balance          TEXT,              -- LEFT / CENTER / RIGHT

    blink_rate       REAL,
    face_distance_cm REAL,
    static_hold_sec  REAL,

    raw_data         JSONB              -- 원본. 위 컬럼으로 못 뽑는 것만
);

CREATE INDEX IF NOT EXISTS idx_sensor_logs_user_t ON sensor_logs (user_name, t DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_logs_t      ON sensor_logs (t DESC);

-- 깜빡임: 초 단위 사건. 낱개로 넣고 집계는 쿼리에서 합니다
CREATE TABLE IF NOT EXISTS blink_events (
    id        BIGSERIAL PRIMARY KEY,
    t         TIMESTAMPTZ NOT NULL,
    user_name TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blink_user_t ON blink_events (user_name, t DESC);

-- 세션 보고서
CREATE TABLE IF NOT EXISTS reports (
    id         BIGSERIAL PRIMARY KEY,
    user_name  TEXT        NOT NULL,
    kind       TEXT        NOT NULL DEFAULT 'session',
    start_t    TIMESTAMPTZ NOT NULL,
    end_t      TIMESTAMPTZ NOT NULL,
    summary    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reports_user ON reports (user_name, end_t DESC);

-- ── 앱 전용 롤 ────────────────────────────────────────────────────────
-- postgres 슈퍼유저로 접속하지 마세요.
--
--   CREATE ROLE soma_app LOGIN PASSWORD '<강한 비밀번호>';
--   GRANT SELECT, INSERT ON sensor_logs, blink_events, reports TO soma_app;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO soma_app;
