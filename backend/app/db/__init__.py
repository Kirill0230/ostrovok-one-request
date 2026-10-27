"""Подключение к Postgres: пул соединений и SQL-миграции.

Настройки (переменные окружения):
  DATABASE_URL  — строка подключения (по умолчанию — контейнер из docker-compose.yml);
  DB_SCHEMA     — схема Postgres (по умолчанию public; eval создаёт себе временную).

Миграции — файлы migrations/NNN_*.sql, применяются по порядку один раз (таблица schema_migrations).
Новая миграция = новый файл со следующим номером; старые файлы не редактируем.
"""
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_URL = "postgresql://odin:odin@localhost:5544/odin"

_pool: Optional[ConnectionPool] = None
_lock = threading.Lock()


def schema() -> str:
    name = os.getenv("DB_SCHEMA", "public")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"Недопустимое имя схемы: {name!r}")
    return name


def _configure(conn: psycopg.Connection) -> None:
    conn.execute(f"SET search_path TO {schema()}")
    conn.commit()


def pool() -> ConnectionPool:
    """Пул создаётся при первом обращении; заодно применяются миграции."""
    global _pool
    with _lock:
        if _pool is None:
            url = os.getenv("DATABASE_URL", DEFAULT_URL)
            with psycopg.connect(url, autocommit=True) as c:
                c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema()}")
            p = ConnectionPool(url, min_size=1, max_size=int(os.getenv("DB_POOL_SIZE", "20")),
                               kwargs={"row_factory": dict_row}, configure=_configure, open=True)
            _migrate(p)
            _pool = p
    return _pool


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """Транзакция: commit при успехе, rollback при исключении."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def _migrate(p: ConnectionPool) -> None:
    with p.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7212001)")  # два процесса не мигрируют одновременно
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(f.name.split("_", 1)[0])
            if version in done:
                continue
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, f.name))


def close() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def drop_schema() -> None:
    """Удалить текущую схему целиком — только для временных схем (eval, тесты)."""
    if schema() == "public":
        raise RuntimeError("Схему public не удаляем")
    close()
    with psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_URL), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema()} CASCADE")
