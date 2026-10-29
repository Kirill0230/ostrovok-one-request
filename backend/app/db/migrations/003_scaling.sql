ALTER TABLE sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE maintenance_runs (
    job          TEXT PRIMARY KEY,
    last_run_at  TIMESTAMPTZ NOT NULL,
    last_result  JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE analytics_events (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_ref  TEXT NOT NULL,
    event        TEXT NOT NULL,
    stage        TEXT,
    props        JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX analytics_events_at_idx ON analytics_events (at);
CREATE INDEX analytics_events_event_idx ON analytics_events (event, at);
CREATE INDEX analytics_events_session_idx ON analytics_events (session_ref);

DROP TABLE rate_limits;
