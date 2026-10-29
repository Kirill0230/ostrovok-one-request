import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_URL = "postgresql://odin:odin@localhost:5544/odin"

_pool: Optional[AsyncConnectionPool] = None
_lock = asyncio.Lock()


def url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_URL)


def schema() -> str:
    name = os.getenv("DB_SCHEMA", "public")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"Недопустимое имя схемы: {name!r}")
    return name


async def _configure(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(f"SET search_path TO {schema()}")
    await conn.commit()


def migrate() -> None:
    with psycopg.connect(url(), autocommit=True) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema()}")
    with psycopg.connect(url(), row_factory=dict_row) as conn, conn.transaction():
        conn.execute(f"SET LOCAL search_path TO {schema()}")
        conn.execute("SELECT pg_advisory_xact_lock(7212001)")
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


async def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                await asyncio.to_thread(migrate)
                p = AsyncConnectionPool(url(), min_size=1, max_size=int(os.getenv("DB_POOL_SIZE", "20")),
                                        kwargs={"row_factory": dict_row}, configure=_configure, open=False)
                await p.open()
                _pool = p
    return _pool


@asynccontextmanager
async def tx() -> AsyncIterator[psycopg.AsyncConnection]:
    async with (await pool()).connection() as conn:
        async with conn.transaction():
            yield conn


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def drop_schema() -> None:
    if schema() == "public":
        raise RuntimeError("Схему public не удаляем")
    with psycopg.connect(url(), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema()} CASCADE")
