-- 004_add_state_logs_session_id.sql
--
-- 인증된 measurement 회차를 state_logs에서 구분합니다.
-- migration 이전 행을 보존하기 위해 session_id는 nullable입니다.
-- 신규 ACTIVE session은 애플리케이션에서 user_id/session_id를 모두 기록합니다.

ALTER TABLE public.state_logs
    ADD COLUMN IF NOT EXISTS session_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'state_logs_session_requires_user_check'
           AND conrelid = 'public.state_logs'::regclass
    ) THEN
        ALTER TABLE public.state_logs
            ADD CONSTRAINT state_logs_session_requires_user_check
            CHECK (
                (user_id IS NULL AND session_id IS NULL)
                OR (user_id IS NOT NULL AND session_id IS NOT NULL)
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_state_logs_user_session_measured_at
    ON public.state_logs (user_id, session_id, measured_at ASC, id ASC)
    WHERE user_id IS NOT NULL
      AND session_id IS NOT NULL;
