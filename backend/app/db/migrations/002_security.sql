ALTER TABLE sessions ALTER COLUMN state DROP NOT NULL;
ALTER TABLE sessions ADD COLUMN state_enc TEXT;

ALTER TABLE messages ALTER COLUMN text DROP NOT NULL;
ALTER TABLE messages ADD COLUMN text_enc TEXT;

ALTER TABLE profiles ALTER COLUMN profile DROP NOT NULL;
ALTER TABLE profiles ADD COLUMN profile_enc TEXT;

ALTER TABLE bookings ADD COLUMN contact_enc TEXT;
ALTER TABLE bookings ADD COLUMN pii_erased_at TIMESTAMPTZ;

CREATE TABLE consents (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    granted         BOOLEAN NOT NULL,
    ip_hash         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX consents_user_idx ON consents (user_id);

CREATE TABLE privacy_log (
    id          BIGSERIAL PRIMARY KEY,
    user_hash   TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('export', 'erase', 'retention')),
    details     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE rate_limits (
    key           TEXT NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    count         INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX rate_limits_window_idx ON rate_limits (window_start);
