"""Общие мелочи агента: «сегодня» и контекст одного хода."""
import os
from dataclasses import dataclass, field
from datetime import date
from typing import List

from ..models import AgentStep


def today() -> date:
    fake = os.getenv("FAKE_TODAY")
    return date.fromisoformat(fake) if fake else date.today()


@dataclass
class Ctx:
    """Что накопилось за один ход: шаги для лога, изменения плана, заметки, быстрые ответы, начало ответа."""
    steps: List[AgentStep] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    quick: List[str] = field(default_factory=list)
    head: List[str] = field(default_factory=list)

    def step(self, tool: str, summary: str):
        self.steps.append(AgentStep(tool=tool, summary=summary))
