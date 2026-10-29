CREATE TABLE profiles (
    user_id     TEXT PRIMARY KEY,
    profile     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id             TEXT PRIMARY KEY,
    user_id        TEXT,
    state          JSONB NOT NULL,
    state_version  INTEGER NOT NULL,
    deck           JSONB NOT NULL DEFAULT '[]',
    metrics        JSONB NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

CREATE TABLE messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'agent')),
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messages_session_idx ON messages (session_id, id);

CREATE TABLE bookings (
    id              TEXT PRIMARY KEY,
    session_id      TEXT REFERENCES sessions (id) ON DELETE SET NULL,
    user_id         TEXT,
    status          TEXT NOT NULL CHECK (status IN ('confirmed', 'changed', 'cancelled')),
    destination_id  TEXT NOT NULL,
    check_in        DATE,
    check_out       DATE,
    total           INTEGER NOT NULL,
    contact_name    TEXT,
    contact_email   TEXT,
    details         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bookings_user_idx ON bookings (user_id);
CREATE INDEX bookings_status_idx ON bookings (status);

CREATE TABLE booking_events (
    id            BIGSERIAL PRIMARY KEY,
    booking_id    TEXT NOT NULL REFERENCES bookings (id) ON DELETE CASCADE,
    type          TEXT NOT NULL CHECK (type IN ('created', 'changed', 'cancelled')),
    amount_delta  INTEGER NOT NULL DEFAULT 0,
    total_after   INTEGER NOT NULL,
    description   TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX booking_events_booking_idx ON booking_events (booking_id, id);
