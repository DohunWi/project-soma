-- 003_create_state_logs.sql
--
-- Fusion decision snapshot 전용 테이블입니다.
-- 기존 sensor_logs는 legacy 데이터 보존용이므로 변경하지 않습니다.
--
-- measured_at: Fusion decision이 만들어진 실제 측정 시각
-- created_at:  DB가 INSERT를 받은 시각
-- 네트워크 장애 후 재시도해도 두 시각을 합치지 않습니다.

CREATE TABLE public.state_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    user_id UUID,
    user_name TEXT NOT NULL,
    device_id TEXT NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger TEXT NOT NULL,
    state TEXT NOT NULL,
    score SMALLINT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    reasons TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    balance TEXT,
    static_hold_sec DOUBLE PRECISION,
    low_blink_sec DOUBLE PRECISION,
    session_sec DOUBLE PRECISION,
    chair_distance_mm INTEGER,
    blink_rate DOUBLE PRECISION,
    face_distance_cm DOUBLE PRECISION,

    CONSTRAINT state_logs_trigger_check
        CHECK (trigger IN ('state_change', 'periodic')),
    CONSTRAINT state_logs_state_check
        CHECK (state IN ('NORMAL', 'CAUTION', 'DANGER', 'ABSENT')),
    CONSTRAINT state_logs_score_check
        CHECK (score BETWEEN 0 AND 100),
    CONSTRAINT state_logs_confidence_check
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    CONSTRAINT state_logs_reasons_check
        CHECK (reasons <@ ARRAY[
            'low_blink',
            'close_distance',
            'imbalance',
            'static_hold',
            'no_backrest'
        ]::TEXT[]),
    CONSTRAINT state_logs_balance_check
        CHECK (balance IS NULL OR balance IN ('LEFT', 'CENTER', 'RIGHT')),
    CONSTRAINT state_logs_static_hold_sec_check
        CHECK (static_hold_sec IS NULL OR static_hold_sec >= 0.0),
    CONSTRAINT state_logs_low_blink_sec_check
        CHECK (low_blink_sec IS NULL OR low_blink_sec >= 0.0),
    CONSTRAINT state_logs_session_sec_check
        CHECK (session_sec IS NULL OR session_sec >= 0.0),
    CONSTRAINT state_logs_chair_distance_mm_check
        CHECK (chair_distance_mm IS NULL OR chair_distance_mm >= 0),
    CONSTRAINT state_logs_blink_rate_check
        CHECK (blink_rate IS NULL OR blink_rate >= 0.0),
    CONSTRAINT state_logs_face_distance_cm_check
        CHECK (face_distance_cm IS NULL OR face_distance_cm > 0.0)
);

-- user_id가 없는 현재 Chair-only guest 행은 이 인덱스에 넣지 않습니다.
-- 인증 사용자의 최근 구간 조회와 장기 보고서에 사용합니다.
CREATE INDEX idx_state_logs_user_measured_at
    ON public.state_logs (user_id, measured_at DESC)
    WHERE user_id IS NOT NULL;

-- user_id가 없는 Demo/guest 경로와 장치별 최근 구간 조회에 사용합니다.
CREATE INDEX idx_state_logs_device_measured_at
    ON public.state_logs (device_id, measured_at DESC);

-- 인증 역할과 Backend 전용 INSERT 정책은 아직 확정되지 않았습니다.
-- 허용 policy를 추측해서 만들지 않고, 현재는 policy 0개의 deny-by-default로 둡니다.
ALTER TABLE public.state_logs ENABLE ROW LEVEL SECURITY;
