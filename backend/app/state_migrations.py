"""Миграции сохранённого состояния поездки (TripState) между версиями схемы.

Сессия хранится в базе JSON-ом вместе с номером версии. При загрузке старой сессии
функции ниже по цепочке приводят JSON к текущей STATE_VERSION, и только потом его читает pydantic.

Как поменять схему несовместимо (переименовать поле, сменить тип):
  1. увеличьте STATE_VERSION в models.py (например, 1 → 2);
  2. добавьте сюда функцию «из 1 в 2» и впишите её в MIGRATIONS под ключом 1.
Добавление нового поля со значением по умолчанию миграции не требует.

Пример:
    def _v1_to_v2(d: dict) -> dict:
        d["travellers"]["infants"] = d["travellers"].pop("babies", 0)   # переименовали поле
        return d
    MIGRATIONS = {1: _v1_to_v2}
"""
from typing import Callable, Dict

from .models import STATE_VERSION

MIGRATIONS: Dict[int, Callable[[dict], dict]] = {}


class StateTooNew(Exception):
    """Сессия записана более новой версией кода (например, после отката деплоя)."""


def upgrade(data: dict, version: int) -> dict:
    if version > STATE_VERSION:
        raise StateTooNew(f"версия состояния {version} новее кода ({STATE_VERSION})")
    while version < STATE_VERSION:
        data = MIGRATIONS[version](data)
        version += 1
    return data
