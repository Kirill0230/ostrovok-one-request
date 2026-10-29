import logging
import math
import os
import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, Request
from redis.exceptions import RedisError

from .. import config
from .auth import Identity
from .crypto import pseudonymize

log = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
_client: Optional[aioredis.Redis] = None


def client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def hit(key: str, limit: int, window_s: int) -> None:
    if not config.RATE_LIMIT_ENABLED or limit <= 0:
        return
    now = time.time()
    window = math.floor(now / window_s)
    bucket = f"rl:{key}:{window}"
    try:
        async with client().pipeline(transaction=True) as pipe:
            count, _ = await pipe.incr(bucket).expire(bucket, window_s + 5).execute()
    except RedisError:
        log.error("Redis недоступен — лимит %s не проверен", key.split(":")[0], exc_info=True)
        return
    if count > limit:
        retry = int((window + 1) * window_s - now) + 1
        raise HTTPException(429, "Слишком много запросов — попробуйте чуть позже", headers={"Retry-After": str(retry)})


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def limit_agent_call(user: Identity) -> None:
    await hit(f"agent:min:{user.user_id}", config.LIMIT_AGENT_PER_MIN, 60)
    await hit(f"agent:day:{user.user_id}", config.LIMIT_AGENT_PER_DAY, 86400)
    await hit("agent:global:day", config.LIMIT_AGENT_GLOBAL_PER_DAY, 86400)


async def limit_new_session(user: Identity) -> None:
    await hit(f"session:hour:{user.user_id}", config.LIMIT_SESSIONS_PER_HOUR, 3600)


async def limit_auth(request: Request) -> None:
    await hit(f"auth:ip:{pseudonymize(client_ip(request))}", config.LIMIT_AUTH_PER_IP_PER_MIN, 60)
