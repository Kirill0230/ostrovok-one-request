import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

import jwt
from fastapi import Header, HTTPException

from .. import config

ALGORITHM = "HS256"
ISSUER = "odin-zapros"


@dataclass(frozen=True)
class Identity:
    user_id: str
    kind: str


def issue_token(user_id: str, kind: str) -> str:
    ttl_days = config.TELEGRAM_TOKEN_TTL_DAYS if kind == "telegram" else config.ANON_TOKEN_TTL_DAYS
    now = int(time.time())
    return jwt.encode({"sub": user_id, "kind": kind, "iat": now, "exp": now + ttl_days * 86400, "iss": ISSUER},
                      config.AUTH_SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> Identity:
    try:
        data = jwt.decode(token, config.AUTH_SECRET, algorithms=[ALGORITHM], issuer=ISSUER,
                          options={"require": ["sub", "exp", "iat", "iss"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Недействительный или истёкший токен", headers={"WWW-Authenticate": "Bearer"})
    return Identity(user_id=data["sub"], kind=data.get("kind", "anon"))


def new_anonymous_id() -> str:
    return "anon-" + secrets.token_hex(12)


def current_user(authorization: Optional[str] = Header(default=None)) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Нужна авторизация", headers={"WWW-Authenticate": "Bearer"})
    return verify_token(authorization[7:].strip())


class TelegramAuthError(Exception):
    pass


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_s: int, now: Optional[float] = None) -> dict:
    if not bot_token:
        raise TelegramAuthError("вход через Telegram не настроен (TELEGRAM_BOT_TOKEN)")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise TelegramAuthError("нет подписи")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise TelegramAuthError("подпись не сходится")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise TelegramAuthError("некорректная дата")
    if (now or time.time()) - auth_date > max_age_s:
        raise TelegramAuthError("данные устарели — откройте приложение заново")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise TelegramAuthError("некорректные данные пользователя")
    if not isinstance(user, dict) or not user.get("id"):
        raise TelegramAuthError("нет пользователя")
    return user
