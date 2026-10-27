"""Лимиты запросов в Postgres (работают и при нескольких экземплярах сервера).

Счётчик фиксированного окна: ключ + начало окна → число запросов. Старые окна удаляет фоновая очистка.
"""
import math
import time

from fastapi import HTTPException, Request

from .. import config, db
from .auth import Identity
from .crypto import pseudonymize


def hit(key: str, limit: int, window_s: int) -> None:
    """Засчитать запрос; при превышении — 429 с Retry-After."""
    if not config.RATE_LIMIT_ENABLED or limit <= 0:
        return
    now = time.time()
    start = math.floor(now / window_s) * window_s
    with db.tx() as conn:
        count = conn.execute(
            """INSERT INTO rate_limits (key, window_start, count) VALUES (%s, to_timestamp(%s), 1)
               ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1
               RETURNING count""", (key, start)).fetchone()["count"]
    if count > limit:
        retry = int(start + window_s - now) + 1
        raise HTTPException(429, "Слишком много запросов — попробуйте чуть позже",
                            headers={"Retry-After": str(retry)})


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limit_agent_call(user: Identity) -> None:
    """Каждое обращение к агенту (а с LLM — это токены): лимит в минуту, в сутки и общий потолок сервиса."""
    hit(f"agent:min:{user.user_id}", config.LIMIT_AGENT_PER_MIN, 60)
    hit(f"agent:day:{user.user_id}", config.LIMIT_AGENT_PER_DAY, 86400)
    hit("agent:global:day", config.LIMIT_AGENT_GLOBAL_PER_DAY, 86400)


def limit_new_session(user: Identity) -> None:
    hit(f"session:hour:{user.user_id}", config.LIMIT_SESSIONS_PER_HOUR, 3600)


def limit_auth(request: Request) -> None:
    hit(f"auth:ip:{pseudonymize(client_ip(request))}", config.LIMIT_AUTH_PER_IP_PER_MIN, 60)
