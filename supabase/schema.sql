-- ============================================================================
-- AI Video Studio - Supabase PostgreSQL Schema, Triggers, RPCs, RLS & Storage
-- ============================================================================
-- Compatible with: Supabase PostgreSQL 15+ (Supabase SQL Editor / CLI migrations)
-- Stage 1 Architecture:
--   - Profiles & subscription tiers (free | pro | enterprise)
--   - Atomic credit wallet (user_credits) + immutable audit trail (credit_ledger)
--   - Unified projects table (translator & faceless modes, AWAITING_REVIEW HITL)
--   - Unified scenes/subtitles table (project_scenes with word_timestamps for .ass)
--   - Payment transactions (SePay / Casso / VietQR webhook idempotency)
--   - SECURITY DEFINER atomic RPCs (service_role only)
--   - Strict Row Level Security (RLS) + user-scoped Storage Bucket policies
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. EXTENSIONS
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------------------------------------------------------
-- 1. TABLES
-- ----------------------------------------------------------------------------

-- 1.1 User Profiles (1:1 with auth.users)
CREATE TABLE IF NOT EXISTS public.profiles (
    id                UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email             TEXT,
    display_name      TEXT,
    avatar_url        TEXT,
    subscription_tier TEXT NOT NULL DEFAULT 'free'
                          CHECK (subscription_tier IN ('free', 'pro', 'enterprise')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.profiles IS
    'User profile metadata and subscription tier (pro/enterprise bypass free-tier watermark).';

-- 1.2 User Credits Wallet (1:1 with auth.users; mutated ONLY via SECURITY DEFINER RPCs / service_role)
CREATE TABLE IF NOT EXISTS public.user_credits (
    user_id            UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    balance            INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    lifetime_purchased INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_purchased >= 0),
    lifetime_spent     INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_spent >= 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.user_credits IS
    'Authoritative user credit balance. Never directly writable from client JWT.';

-- 1.3 Video Projects (supports both AI Video Translator & AI Faceless Generator)
CREATE TABLE IF NOT EXISTS public.projects (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    title             TEXT NOT NULL DEFAULT 'Untitled Project',
    mode              TEXT NOT NULL CHECK (mode IN ('translator', 'faceless')),
    status            TEXT NOT NULL DEFAULT 'PENDING'
                          CHECK (status IN (
                              'PENDING',
                              'PROCESSING',
                              'AWAITING_REVIEW',
                              'RENDERING',
                              'COMPLETED',
                              'FAILED',
                              'CANCELLED'
                          )),
    aspect_ratio      TEXT NOT NULL DEFAULT '9:16'
                          CHECK (aspect_ratio IN ('9:16', '16:9', '1:1')),
    source_mode       TEXT,
    source_inputs     JSONB NOT NULL DEFAULT '{}'::jsonb,
    pipeline_config   JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress_pct      INTEGER NOT NULL DEFAULT 0
                          CHECK (progress_pct >= 0 AND progress_pct <= 100),
    current_stage     TEXT,
    output_video_url  TEXT,
    duration_seconds  NUMERIC(10, 2)
                          CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    credits_estimated INTEGER NOT NULL DEFAULT 0 CHECK (credits_estimated >= 0),
    credits_charged   INTEGER NOT NULL DEFAULT 0 CHECK (credits_charged >= 0),
    error_message     TEXT,
    batch_id          UUID,
    temp_cleaned_at   TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.projects IS
    'Video generation/translation jobs with pipeline_config JSONB, SSE progress tracking, AWAITING_REVIEW state, and temp cleanup timestamps.';

-- 1.4 Project Scenes / Subtitle Segments (unified for Faceless scenes & Translator lines)
CREATE TABLE IF NOT EXISTS public.project_scenes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scene_index     INTEGER NOT NULL CHECK (scene_index >= 0),
    start_ms        INTEGER NOT NULL DEFAULT 0 CHECK (start_ms >= 0),
    end_ms          INTEGER NOT NULL DEFAULT 0 CHECK (end_ms >= start_ms),
    original_text   TEXT,
    edited_text     TEXT,
    narration_text  TEXT,
    word_timestamps JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_keywords TEXT,
    image_prompt    TEXT,
    emotion_tone    TEXT,
    speaker_label   TEXT,
    voice_id        TEXT,
    tts_rate_pct    INTEGER NOT NULL DEFAULT 0
                        CHECK (tts_rate_pct >= -50 AND tts_rate_pct <= 100),
    audio_url       TEXT,
    visual_url      TEXT,
    visual_source   TEXT CHECK (
                        visual_source IS NULL
                        OR visual_source IN ('pexels', 'pixabay', 'ai_gen', 'custom_upload')
                    ),
    subtitle_style  JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_locked       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_project_scenes_project_index UNIQUE (project_id, scene_index)
);

COMMENT ON TABLE public.project_scenes IS
    'Unified timeline items for Faceless storyboard scenes and Translator subtitle segments, including word_timestamps for .ass karaoke rendering.';

-- 1.5 Payment Transactions (SePay / Casso / VietQR bank transfer webhooks)
CREATE TABLE IF NOT EXISTS public.payment_transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    gateway         TEXT NOT NULL CHECK (gateway IN ('sepay', 'casso', 'vietqr', 'manual', 'admin')),
    reference_code  TEXT NOT NULL UNIQUE,
    external_tx_id  TEXT UNIQUE,
    amount_vnd      BIGINT NOT NULL CHECK (amount_vnd > 0),
    credits_amount  INTEGER NOT NULL CHECK (credits_amount > 0),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'failed', 'expired')),
    raw_payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

COMMENT ON TABLE public.payment_transactions IS
    'VietQR / SePay / Casso top-up transactions with reference_code memo matching and external_tx_id webhook idempotency.';

-- 1.6 Immutable Credit Ledger (Append-only audit trail)
CREATE TABLE IF NOT EXISTS public.credit_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    entry_type      TEXT NOT NULL
                        CHECK (entry_type IN ('signup_bonus', 'topup', 'deduction', 'refund', 'adjustment')),
    amount          INTEGER NOT NULL CHECK (amount <> 0),
    balance_after   INTEGER NOT NULL CHECK (balance_after >= 0),
    project_id      UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    transaction_id  UUID REFERENCES public.payment_transactions(id) ON DELETE SET NULL,
    description     TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.credit_ledger IS
    'Immutable audit trail of every credit bonus, top-up, deduction, refund, and adjustment.';

-- ----------------------------------------------------------------------------
-- 2. INDEXES
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_projects_user_created
    ON public.projects (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_projects_status
    ON public.projects (status);

CREATE INDEX IF NOT EXISTS idx_projects_batch_id
    ON public.projects (batch_id)
    WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_projects_temp_cleanup
    ON public.projects (temp_cleaned_at, created_at)
    WHERE temp_cleaned_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_project_scenes_project_order
    ON public.project_scenes (project_id, scene_index);

CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_created
    ON public.payment_transactions (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_payment_transactions_status
    ON public.payment_transactions (status);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_created
    ON public.credit_ledger (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_project
    ON public.credit_ledger (project_id)
    WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_credit_ledger_transaction
    ON public.credit_ledger (transaction_id)
    WHERE transaction_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 3. TRIGGER FUNCTIONS & TRIGGERS
-- ----------------------------------------------------------------------------

-- 3.1 Generic updated_at timestamp maintenance
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON public.profiles;
CREATE TRIGGER trg_profiles_updated_at
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_user_credits_updated_at ON public.user_credits;
CREATE TRIGGER trg_user_credits_updated_at
    BEFORE UPDATE ON public.user_credits
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_projects_updated_at ON public.projects;
CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON public.projects
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_project_scenes_updated_at ON public.project_scenes;
CREATE TRIGGER trg_project_scenes_updated_at
    BEFORE UPDATE ON public.project_scenes
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_payment_transactions_updated_at ON public.payment_transactions;
CREATE TRIGGER trg_payment_transactions_updated_at
    BEFORE UPDATE ON public.payment_transactions
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

-- 3.2 Prevent client JWT from self-upgrading subscription_tier
CREATE OR REPLACE FUNCTION public.protect_profile_sensitive_columns()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_jwt_role TEXT := COALESCE(
        NULLIF(current_setting('request.jwt.claim.role', true), ''),
        NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role',
        ''
    );
BEGIN
    IF current_user IN ('anon', 'authenticated') OR v_jwt_role IN ('anon', 'authenticated') THEN
        IF NEW.subscription_tier IS DISTINCT FROM OLD.subscription_tier THEN
            RAISE EXCEPTION 'Permission denied: subscription_tier can only be changed by service_role';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_profiles_protect_sensitive ON public.profiles;
CREATE TRIGGER trg_profiles_protect_sensitive
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.protect_profile_sensitive_columns();

-- 3.3 Auto-provision profile, initial credits (default 50), and ledger bonus on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_initial_credits INTEGER;
    v_display_name    TEXT;
    v_avatar_url      TEXT;
    v_credits_inserted UUID;
BEGIN
    v_initial_credits := COALESCE(
        NULLIF(current_setting('app.initial_free_credits', true), '')::INTEGER,
        50
    );

    v_display_name := COALESCE(
        NULLIF(NEW.raw_user_meta_data ->> 'full_name', ''),
        NULLIF(NEW.raw_user_meta_data ->> 'name', ''),
        NULLIF(split_part(COALESCE(NEW.email, ''), '@', 1), ''),
        'Creator'
    );

    v_avatar_url := NULLIF(NEW.raw_user_meta_data ->> 'avatar_url', '');

    INSERT INTO public.profiles (id, email, display_name, avatar_url, subscription_tier)
    VALUES (NEW.id, NEW.email, v_display_name, v_avatar_url, 'free')
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO public.user_credits (user_id, balance, lifetime_purchased, lifetime_spent)
    VALUES (NEW.id, v_initial_credits, 0, 0)
    ON CONFLICT (user_id) DO NOTHING
    RETURNING user_id INTO v_credits_inserted;

    IF v_credits_inserted IS NOT NULL AND v_initial_credits > 0 THEN
        INSERT INTO public.credit_ledger (
            user_id,
            entry_type,
            amount,
            balance_after,
            description,
            metadata
        )
        VALUES (
            NEW.id,
            'signup_bonus',
            v_initial_credits,
            v_initial_credits,
            'Initial free credits on signup',
            jsonb_build_object('source', 'handle_new_user')
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();

-- ----------------------------------------------------------------------------
-- 4. ATOMIC BILLING & CREDIT RPC FUNCTIONS (SECURITY DEFINER)
-- ----------------------------------------------------------------------------

-- 4.1 Atomic credit deduction with row lock (FOR UPDATE) + credit_ledger audit
CREATE OR REPLACE FUNCTION public.deduct_user_credits(
    p_user_id     UUID,
    p_amount      INTEGER,
    p_project_id  UUID DEFAULT NULL,
    p_description TEXT DEFAULT 'Video processing credit deduction',
    p_metadata    JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_prev_balance INTEGER;
    v_new_balance  INTEGER;
    v_ledger_id    UUID;
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION 'p_user_id is required';
    END IF;

    IF p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'p_amount must be a positive integer, got %', p_amount;
    END IF;

    -- Lock wallet row to prevent concurrent double-spend races
    SELECT balance
      INTO v_prev_balance
      FROM public.user_credits
     WHERE user_id = p_user_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Credit wallet not found for user %', p_user_id;
    END IF;

    IF v_prev_balance < p_amount THEN
        RAISE EXCEPTION 'Insufficient credits: balance=%, required=%', v_prev_balance, p_amount
            USING ERRCODE = 'P0001';
    END IF;

    UPDATE public.user_credits
       SET balance = balance - p_amount,
           lifetime_spent = lifetime_spent + p_amount
     WHERE user_id = p_user_id
     RETURNING balance INTO v_new_balance;

    IF p_project_id IS NOT NULL THEN
        UPDATE public.projects
           SET credits_charged = credits_charged + p_amount
         WHERE id = p_project_id
           AND user_id = p_user_id;
    END IF;

    INSERT INTO public.credit_ledger (
        user_id,
        entry_type,
        amount,
        balance_after,
        project_id,
        description,
        metadata
    )
    VALUES (
        p_user_id,
        'deduction',
        -p_amount,
        v_new_balance,
        p_project_id,
        COALESCE(NULLIF(p_description, ''), 'Video processing credit deduction'),
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_ledger_id;

    RETURN jsonb_build_object(
        'status', 'deducted',
        'user_id', p_user_id,
        'deducted', p_amount,
        'previous_balance', v_prev_balance,
        'new_balance', v_new_balance,
        'ledger_id', v_ledger_id
    );
END;
$$;

-- 4.2 Atomic credit refund with row lock (FOR UPDATE) + credit_ledger audit
CREATE OR REPLACE FUNCTION public.refund_user_credits(
    p_user_id     UUID,
    p_amount      INTEGER,
    p_project_id  UUID DEFAULT NULL,
    p_description TEXT DEFAULT 'Credit refund',
    p_metadata    JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_prev_balance INTEGER;
    v_new_balance  INTEGER;
    v_ledger_id    UUID;
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION 'p_user_id is required';
    END IF;

    IF p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'p_amount must be a positive integer, got %', p_amount;
    END IF;

    INSERT INTO public.user_credits (user_id, balance, lifetime_purchased, lifetime_spent)
    VALUES (p_user_id, 0, 0, 0)
    ON CONFLICT (user_id) DO NOTHING;

    SELECT balance
      INTO v_prev_balance
      FROM public.user_credits
     WHERE user_id = p_user_id
       FOR UPDATE;

    UPDATE public.user_credits
       SET balance = balance + p_amount,
           lifetime_spent = GREATEST(0, lifetime_spent - p_amount)
     WHERE user_id = p_user_id
     RETURNING balance INTO v_new_balance;

    IF p_project_id IS NOT NULL THEN
        UPDATE public.projects
           SET credits_charged = GREATEST(0, credits_charged - p_amount)
         WHERE id = p_project_id
           AND user_id = p_user_id;
    END IF;

    INSERT INTO public.credit_ledger (
        user_id,
        entry_type,
        amount,
        balance_after,
        project_id,
        description,
        metadata
    )
    VALUES (
        p_user_id,
        'refund',
        p_amount,
        v_new_balance,
        p_project_id,
        COALESCE(NULLIF(p_description, ''), 'Credit refund'),
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_ledger_id;

    RETURN jsonb_build_object(
        'status', 'refunded',
        'user_id', p_user_id,
        'refunded', p_amount,
        'previous_balance', v_prev_balance,
        'new_balance', v_new_balance,
        'ledger_id', v_ledger_id
    );
END;
$$;

-- 4.3 Atomic, idempotent payment webhook processor (SePay / Casso / VietQR)
CREATE OR REPLACE FUNCTION public.process_payment_webhook(
    p_reference_code TEXT,
    p_external_tx_id TEXT,
    p_amount_vnd     BIGINT,
    p_gateway        TEXT DEFAULT 'sepay',
    p_credits_amount INTEGER DEFAULT NULL,
    p_user_id        UUID DEFAULT NULL,
    p_raw_payload    JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_tx            public.payment_transactions%ROWTYPE;
    v_tx_id         UUID;
    v_target_user   UUID;
    v_credits       INTEGER;
    v_prev_balance  INTEGER;
    v_new_balance   INTEGER;
    v_ledger_id     UUID;
BEGIN
    IF p_reference_code IS NULL OR btrim(p_reference_code) = '' THEN
        RAISE EXCEPTION 'p_reference_code is required';
    END IF;

    IF p_external_tx_id IS NULL OR btrim(p_external_tx_id) = '' THEN
        RAISE EXCEPTION 'p_external_tx_id is required for webhook idempotency';
    END IF;

    IF p_amount_vnd IS NULL OR p_amount_vnd <= 0 THEN
        RAISE EXCEPTION 'p_amount_vnd must be positive, got %', p_amount_vnd;
    END IF;

    -- Serialize concurrent identical webhooks for the same external_tx_id / reference_code
    PERFORM pg_advisory_xact_lock(hashtext('tx_ext:' || p_external_tx_id));
    PERFORM pg_advisory_xact_lock(hashtext('tx_ref:' || p_reference_code));

    -- 1) Idempotency check by external_tx_id
    SELECT *
      INTO v_tx
      FROM public.payment_transactions
     WHERE external_tx_id = p_external_tx_id
       FOR UPDATE;

    IF FOUND THEN
        IF v_tx.status = 'completed' THEN
            RETURN jsonb_build_object(
                'status', 'already_processed',
                'idempotent', true,
                'transaction_id', v_tx.id,
                'user_id', v_tx.user_id,
                'credits_added', 0
            );
        ELSIF v_tx.reference_code <> p_reference_code THEN
            RAISE EXCEPTION 'external_tx_id % is already bound to a different reference_code (%)',
                p_external_tx_id, v_tx.reference_code;
        END IF;
    END IF;

    -- 2) Lookup by reference_code (transfer memo match)
    SELECT *
      INTO v_tx
      FROM public.payment_transactions
     WHERE reference_code = p_reference_code
       FOR UPDATE;

    IF FOUND THEN
        IF v_tx.status = 'completed' THEN
            RETURN jsonb_build_object(
                'status', 'already_processed',
                'idempotent', true,
                'transaction_id', v_tx.id,
                'user_id', v_tx.user_id,
                'credits_added', 0
            );
        END IF;

        IF p_amount_vnd < v_tx.amount_vnd THEN
            RAISE EXCEPTION 'Transferred amount (% VND) is less than expected (% VND) for reference %',
                p_amount_vnd, v_tx.amount_vnd, p_reference_code;
        END IF;

        v_tx_id       := v_tx.id;
        v_target_user := COALESCE(p_user_id, v_tx.user_id);
        v_credits     := COALESCE(p_credits_amount, v_tx.credits_amount);

        UPDATE public.payment_transactions
           SET external_tx_id = p_external_tx_id,
               gateway        = COALESCE(NULLIF(p_gateway, ''), v_tx.gateway),
               amount_vnd     = p_amount_vnd,
               credits_amount = v_credits,
               status         = 'completed',
               raw_payload    = COALESCE(p_raw_payload, v_tx.raw_payload),
               completed_at   = NOW()
         WHERE id = v_tx_id;
    ELSE
        -- Direct / pre-unregistered VietQR transfer: backend passes resolved user_id & credits_amount
        IF p_user_id IS NULL OR p_credits_amount IS NULL OR p_credits_amount <= 0 THEN
            RAISE EXCEPTION 'No pending transaction for reference_code %, and p_user_id / p_credits_amount were not provided',
                p_reference_code;
        END IF;

        v_target_user := p_user_id;
        v_credits     := p_credits_amount;

        INSERT INTO public.payment_transactions (
            user_id,
            gateway,
            reference_code,
            external_tx_id,
            amount_vnd,
            credits_amount,
            status,
            raw_payload,
            completed_at
        )
        VALUES (
            v_target_user,
            COALESCE(NULLIF(p_gateway, ''), 'sepay'),
            p_reference_code,
            p_external_tx_id,
            p_amount_vnd,
            v_credits,
            'completed',
            COALESCE(p_raw_payload, '{}'::jsonb),
            NOW()
        )
        RETURNING id INTO v_tx_id;
    END IF;

    -- 3) Atomically credit user wallet
    INSERT INTO public.user_credits (user_id, balance, lifetime_purchased, lifetime_spent)
    VALUES (v_target_user, 0, 0, 0)
    ON CONFLICT (user_id) DO NOTHING;

    SELECT balance
      INTO v_prev_balance
      FROM public.user_credits
     WHERE user_id = v_target_user
       FOR UPDATE;

    UPDATE public.user_credits
       SET balance = balance + v_credits,
           lifetime_purchased = lifetime_purchased + v_credits
     WHERE user_id = v_target_user
     RETURNING balance INTO v_new_balance;

    -- 4) Append immutable credit_ledger entry
    INSERT INTO public.credit_ledger (
        user_id,
        entry_type,
        amount,
        balance_after,
        transaction_id,
        description,
        metadata
    )
    VALUES (
        v_target_user,
        'topup',
        v_credits,
        v_new_balance,
        v_tx_id,
        format('Top-up via %s (%s)', COALESCE(NULLIF(p_gateway, ''), 'sepay'), p_reference_code),
        jsonb_build_object(
            'gateway', COALESCE(NULLIF(p_gateway, ''), 'sepay'),
            'reference_code', p_reference_code,
            'external_tx_id', p_external_tx_id,
            'amount_vnd', p_amount_vnd
        )
    )
    RETURNING id INTO v_ledger_id;

    RETURN jsonb_build_object(
        'status', 'completed',
        'idempotent', false,
        'transaction_id', v_tx_id,
        'user_id', v_target_user,
        'credits_added', v_credits,
        'previous_balance', v_prev_balance,
        'new_balance', v_new_balance,
        'ledger_id', v_ledger_id
    );
END;
$$;

-- Restrict sensitive billing RPC execution strictly to service_role
REVOKE ALL ON FUNCTION public.deduct_user_credits(UUID, INTEGER, UUID, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.deduct_user_credits(UUID, INTEGER, UUID, TEXT, JSONB) TO service_role;

REVOKE ALL ON FUNCTION public.refund_user_credits(UUID, INTEGER, UUID, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.refund_user_credits(UUID, INTEGER, UUID, TEXT, JSONB) TO service_role;

REVOKE ALL ON FUNCTION public.process_payment_webhook(TEXT, TEXT, BIGINT, TEXT, INTEGER, UUID, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.process_payment_webhook(TEXT, TEXT, BIGINT, TEXT, INTEGER, UUID, JSONB) TO service_role;

-- ----------------------------------------------------------------------------
-- 5. ROW LEVEL SECURITY (RLS) ON ALL PUBLIC TABLES
-- ----------------------------------------------------------------------------
ALTER TABLE public.profiles             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_credits         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_ledger        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_scenes       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_transactions ENABLE ROW LEVEL SECURITY;

-- 5.1 profiles: users can read & update only their own profile (tier protected by trigger)
DROP POLICY IF EXISTS "profiles_select_own" ON public.profiles;
CREATE POLICY "profiles_select_own"
    ON public.profiles
    FOR SELECT
    TO authenticated
    USING (auth.uid() = id);

DROP POLICY IF EXISTS "profiles_update_own" ON public.profiles;
CREATE POLICY "profiles_update_own"
    ON public.profiles
    FOR UPDATE
    TO authenticated
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

-- 5.2 user_credits: strictly read-only for owner; no client INSERT/UPDATE/DELETE
DROP POLICY IF EXISTS "user_credits_select_own" ON public.user_credits;
CREATE POLICY "user_credits_select_own"
    ON public.user_credits
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

-- 5.3 credit_ledger: strictly read-only for owner; immutable from client JWT
DROP POLICY IF EXISTS "credit_ledger_select_own" ON public.credit_ledger;
CREATE POLICY "credit_ledger_select_own"
    ON public.credit_ledger
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

-- 5.4 projects: full CRUD for owner only
DROP POLICY IF EXISTS "projects_select_own" ON public.projects;
CREATE POLICY "projects_select_own"
    ON public.projects
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "projects_insert_own" ON public.projects;
CREATE POLICY "projects_insert_own"
    ON public.projects
    FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "projects_update_own" ON public.projects;
CREATE POLICY "projects_update_own"
    ON public.projects
    FOR UPDATE
    TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "projects_delete_own" ON public.projects;
CREATE POLICY "projects_delete_own"
    ON public.projects
    FOR DELETE
    TO authenticated
    USING (auth.uid() = user_id);

-- 5.5 project_scenes: full CRUD for owner of parent project
DROP POLICY IF EXISTS "project_scenes_select_own" ON public.project_scenes;
CREATE POLICY "project_scenes_select_own"
    ON public.project_scenes
    FOR SELECT
    TO authenticated
    USING (
        EXISTS (
            SELECT 1
              FROM public.projects p
             WHERE p.id = project_scenes.project_id
               AND p.user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "project_scenes_insert_own" ON public.project_scenes;
CREATE POLICY "project_scenes_insert_own"
    ON public.project_scenes
    FOR INSERT
    TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1
              FROM public.projects p
             WHERE p.id = project_scenes.project_id
               AND p.user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "project_scenes_update_own" ON public.project_scenes;
CREATE POLICY "project_scenes_update_own"
    ON public.project_scenes
    FOR UPDATE
    TO authenticated
    USING (
        EXISTS (
            SELECT 1
              FROM public.projects p
             WHERE p.id = project_scenes.project_id
               AND p.user_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
              FROM public.projects p
             WHERE p.id = project_scenes.project_id
               AND p.user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "project_scenes_delete_own" ON public.project_scenes;
CREATE POLICY "project_scenes_delete_own"
    ON public.project_scenes
    FOR DELETE
    TO authenticated
    USING (
        EXISTS (
            SELECT 1
              FROM public.projects p
             WHERE p.id = project_scenes.project_id
               AND p.user_id = auth.uid()
        )
    );

-- 5.6 payment_transactions: strictly read-only for owner; created/settled by backend service_role
DROP POLICY IF EXISTS "payment_transactions_select_own" ON public.payment_transactions;
CREATE POLICY "payment_transactions_select_own"
    ON public.payment_transactions
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 6. SUPABASE STORAGE BUCKETS & USER-SCOPED FOLDER RLS
-- ----------------------------------------------------------------------------
-- Matches SUPABASE_STORAGE_BUCKET=video-studio-assets in .env.example, plus
-- optional dedicated `outputs` bucket. Files must be stored under:
--   {auth.uid()}/{project_id}/...
-- ----------------------------------------------------------------------------

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES
    (
        'video-studio-assets',
        'video-studio-assets',
        false,
        524288000, -- 500 MB (matches MAX_UPLOAD_MB=500)
        ARRAY[
            'video/mp4',
            'video/webm',
            'video/quicktime',
            'audio/mpeg',
            'audio/wav',
            'audio/mp4',
            'image/jpeg',
            'image/png',
            'image/webp',
            'text/plain',
            'application/json'
        ]
    ),
    (
        'outputs',
        'outputs',
        false,
        524288000,
        ARRAY['video/mp4', 'video/webm', 'image/jpeg', 'image/png', 'text/plain']
    )
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "storage_assets_select_own" ON storage.objects;
CREATE POLICY "storage_assets_select_own"
    ON storage.objects
    FOR SELECT
    TO authenticated
    USING (
        bucket_id IN ('video-studio-assets', 'outputs')
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS "storage_assets_insert_own" ON storage.objects;
CREATE POLICY "storage_assets_insert_own"
    ON storage.objects
    FOR INSERT
    TO authenticated
    WITH CHECK (
        bucket_id IN ('video-studio-assets', 'outputs')
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS "storage_assets_update_own" ON storage.objects;
CREATE POLICY "storage_assets_update_own"
    ON storage.objects
    FOR UPDATE
    TO authenticated
    USING (
        bucket_id IN ('video-studio-assets', 'outputs')
        AND (storage.foldername(name))[1] = auth.uid()::text
    )
    WITH CHECK (
        bucket_id IN ('video-studio-assets', 'outputs')
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS "storage_assets_delete_own" ON storage.objects;
CREATE POLICY "storage_assets_delete_own"
    ON storage.objects
    FOR DELETE
    TO authenticated
    USING (
        bucket_id IN ('video-studio-assets', 'outputs')
        AND (storage.foldername(name))[1] = auth.uid()::text
    );
