from typing import Callable, Dict

from .models import STATE_VERSION

MIGRATIONS: Dict[int, Callable[[dict], dict]] = {}


class StateTooNew(Exception):
    pass


def upgrade(data: dict, version: int) -> dict:
    if version > STATE_VERSION:
        raise StateTooNew(f"версия состояния {version} новее кода ({STATE_VERSION})")
    while version < STATE_VERSION:
        data = MIGRATIONS[version](data)
        version += 1
    return data
