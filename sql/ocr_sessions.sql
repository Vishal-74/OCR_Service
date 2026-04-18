-- ---------------------------------------------------------------------------
-- ocr_sessions
-- ---------------------------------------------------------------------------
-- Persistent session storage for the aiora-ocr-service.
--
-- Key design:
--   - One active OCR conversation per (seller_id, from_phone).
--   - `state` tracks where we are in the confirm/edit flow.
--   - `items_json` holds the current extracted / edited list.
--   - `resume_mode` is the n8n routing mode that was active for this user
--     before the OCR gate took over (order / service / appointment / calling).
--   - `last_message_id` is used for idempotency against Meta's retries.
--   - `expires_at` lets stale sessions self-heal; cleaned up lazily by the
--     service on the next call, or by a cron / scheduled function.
--
-- Run in Supabase SQL Editor (project scoped to the dashboard's database so
-- `whatsapp_config` lives in the same project).
-- ---------------------------------------------------------------------------

-- Drop any legacy `ocr_sessions` table so this script is fully idempotent.
-- The OCR service is the only writer, so it is safe to recreate. If you need
-- to preserve rows in the future, replace this with targeted ALTERs instead.
DROP TABLE IF EXISTS public.ocr_sessions CASCADE;

CREATE TABLE IF NOT EXISTS public.ocr_sessions (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id       TEXT            NOT NULL,
    from_phone      TEXT            NOT NULL,
    state           TEXT            NOT NULL CHECK (state IN ('awaiting_confirmation', 'edit_mode')),
    items_json      JSONB           NOT NULL DEFAULT '[]'::jsonb,
    resume_mode     TEXT            NULL,
    last_message_id TEXT            NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ     NULL,
    CONSTRAINT ocr_sessions_seller_phone_unique UNIQUE (seller_id, from_phone)
);

CREATE INDEX IF NOT EXISTS idx_ocr_sessions_seller_phone
    ON public.ocr_sessions (seller_id, from_phone);

CREATE INDEX IF NOT EXISTS idx_ocr_sessions_expires_at
    ON public.ocr_sessions (expires_at);

-- Optional: auto-update updated_at (same pattern the dashboard uses for
-- whatsapp_config). Safe if the helper function already exists.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'update_ocr_sessions_updated_at'
    ) THEN
        CREATE TRIGGER update_ocr_sessions_updated_at
            BEFORE UPDATE ON public.ocr_sessions
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
    END IF;
EXCEPTION
    WHEN undefined_function THEN
        -- update_updated_at() helper doesn't exist in this project; skip.
        NULL;
END
$$;

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
-- The aiora-ocr-service connects with the Supabase SERVICE ROLE key, which
-- bypasses RLS by design. Enabling RLS with NO policies therefore:
--   * keeps the service working (service_role bypasses RLS),
--   * completely blocks anon and authenticated keys from reading/writing
--     this table (they have no matching policy -> default-deny).
-- This is the recommended configuration for server-only tables.
ALTER TABLE public.ocr_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ocr_sessions FORCE  ROW LEVEL SECURITY;

-- Defense-in-depth: revoke grants from API roles so even a misconfigured
-- policy can't accidentally expose this table via PostgREST.
REVOKE ALL ON public.ocr_sessions FROM anon;
REVOKE ALL ON public.ocr_sessions FROM authenticated;

-- ---------------------------------------------------------------------------
-- Optional cleanup helper (can be scheduled via Supabase cron).
-- ---------------------------------------------------------------------------
-- SELECT cron.schedule(
--   'ocr_sessions_expire',
--   '*/15 * * * *',
--   $$ DELETE FROM public.ocr_sessions WHERE expires_at IS NOT NULL AND expires_at < NOW() $$
-- );
