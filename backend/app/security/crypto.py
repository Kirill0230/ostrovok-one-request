import hashlib
import hmac
import json
from functools import lru_cache
from typing import Any, Optional

from cryptography.fernet import Fernet, MultiFernet

from .. import config


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    if not config.DATA_ENCRYPTION_KEYS:
        raise RuntimeError("DATA_ENCRYPTION_KEYS не задан")
    return MultiFernet([Fernet(k.encode()) for k in config.DATA_ENCRYPTION_KEYS])


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def encrypt_json(value: Any) -> str:
    return encrypt_str(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def decrypt_json(token: Optional[str]) -> Any:
    return json.loads(decrypt_str(token)) if token else None


def pseudonymize(value: str) -> str:
    return hmac.new(config.AUTH_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]
