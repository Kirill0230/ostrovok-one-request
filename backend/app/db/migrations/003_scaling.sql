-- Оптимистичная блокировка: агент работает без открытой транзакции, а при сохранении
-- проверяем, что сессию никто не изменил (UPDATE … WHERE version = прочитанная версия).
ALTER TABLE sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

-- Обслуживание (очистка и т.п.) выполняется одним экземпляром сервиса не чаще заданного интервала
CREATE TABLE maintenance_runs (
    job          TEXT PRIMARY KEY,
    last_run_at  TIMESTAMPTZ NOT NULL,
    last_result  JSONB NOT NULL DEFAULT '{}'
);

-- Обезличенная аналитика: без user_id, текста сообщений, контактов и возраста детей.
-- session_ref — отпечаток id сессии: связывает события одной сессии, но после удаления сессии
-- связь с человеком восстановить нельзя. Удаление данных пользователя аналитику не трогает.
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

-- Лимиты переехали в Redis
DROP TABLE rate_limits;
