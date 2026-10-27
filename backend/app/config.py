"""Настройки приложения из переменных окружения.

В разработке у секретов есть небезопасные значения по умолчанию, чтобы проект запускался без настройки.
В продакшне (APP_ENV=production) сервер не стартует, пока не заданы все секреты и разрешённые домены —
см. validate() и .env.example.
"""
import base64
import hashlib
import os
from typing import List


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> List[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


APP_ENV = _env("APP_ENV", "development")
PROD = APP_ENV == "production"

# --- авторизация
_DEV_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
AUTH_SECRET = _env("AUTH_SECRET") or ("" if PROD else _DEV_SECRET)
ANON_TOKEN_TTL_DAYS = int(_env("ANON_TOKEN_TTL_DAYS", "180"))
TELEGRAM_TOKEN_TTL_DAYS = int(_env("TELEGRAM_TOKEN_TTL_DAYS", "7"))
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_AUTH_MAX_AGE_S = int(_env("TELEGRAM_AUTH_MAX_AGE_S", "86400"))
ADMIN_TOKEN = _env("ADMIN_TOKEN")  # для /api/metrics; пусто — в разработке открыто, в продакшне закрыто

# --- шифрование персональных данных (Fernet). Несколько ключей через запятую: первым шифруем,
# остальными только расшифровываем — так ротируется ключ без потери данных.
_DEV_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"dev-only-data-key").digest()).decode()
DATA_ENCRYPTION_KEYS = _list("DATA_ENCRYPTION_KEYS") or ([] if PROD else [_DEV_KEY])

# --- сеть
CORS_ORIGINS = _list("CORS_ORIGINS", "" if PROD else "http://localhost:5199,http://127.0.0.1:5199")
TRUST_PROXY = _bool("TRUST_PROXY", PROD)  # доверять X-Forwarded-For/Proto (только за своим прокси!)
FORCE_HTTPS = _bool("FORCE_HTTPS", PROD)
MAX_BODY_BYTES = int(_env("MAX_BODY_BYTES", "16384"))

# --- лимиты (защита от перебора и от расхода токенов LLM)
RATE_LIMIT_ENABLED = _bool("RATE_LIMIT_ENABLED", True)
LIMIT_AGENT_PER_MIN = int(_env("LIMIT_AGENT_PER_MIN", "30"))      # сообщения/свайпы одного пользователя
LIMIT_AGENT_PER_DAY = int(_env("LIMIT_AGENT_PER_DAY", "500"))
LIMIT_AGENT_GLOBAL_PER_DAY = int(_env("LIMIT_AGENT_GLOBAL_PER_DAY", "20000"))  # потолок на весь сервис
LIMIT_AUTH_PER_IP_PER_MIN = int(_env("LIMIT_AUTH_PER_IP_PER_MIN", "10"))
LIMIT_SESSIONS_PER_HOUR = int(_env("LIMIT_SESSIONS_PER_HOUR", "30"))

# --- персональные данные (152-ФЗ)
POLICY_VERSION = _env("POLICY_VERSION", "2026-10-27")
BOOKING_PII_RETENTION_DAYS = int(_env("BOOKING_PII_RETENTION_DAYS", "1095"))  # контакты в бронях: 3 года после выезда


def validate() -> None:
    """Вызывается при старте. В продакшне не даём запуститься с дырами в настройках."""
    if not PROD:
        return
    problems = []
    if len(AUTH_SECRET) < 32:
        problems.append("AUTH_SECRET не задан или короче 32 символов")
    if not DATA_ENCRYPTION_KEYS:
        problems.append("DATA_ENCRYPTION_KEYS не задан")
    if not CORS_ORIGINS or "*" in CORS_ORIGINS:
        problems.append("CORS_ORIGINS должен перечислять конкретные домены")
    if not ADMIN_TOKEN:
        problems.append("ADMIN_TOKEN не задан")
    if problems:
        raise RuntimeError("Небезопасная конфигурация продакшна: " + "; ".join(problems))
