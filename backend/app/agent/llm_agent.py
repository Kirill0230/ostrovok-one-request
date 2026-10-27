"""Точка подключения LLM (GigaChat через LangChain) — заполняет LLM-команда.

Агент должен возвращать AgentReply (models.py) — фронт и API от этого не меняются.
Два пути, можно начать с первого и перейти ко второму:

1. Быстрый: LLM только для понимания и текста, оркестрация остаётся детерминированной.
   Переопределите understand(): попросите модель вернуть JSON с полями ParseResult
   (updates/travellers/prefs/add_types/destination/candidates/intents/shift_days).
   Если модель не ответила или вернула мусор — откатываемся на правила (super().understand).
   Переопределите phrase(): модель переписывает черновик живым языком, НЕ меняя цифры.

2. Полный агент: create_react_agent(llm, tools=[...]) с инструментами из app/tools.py
   (suggest_destinations, search_hotels, search_flights, search_transfers) и validator.build_view.
   Состояние — sess.state (TripState); модель меняет его через tool update_trip.
   Карточки для фронта собирайте функциями из agent/cards.py.
"""
from ..models import TripState
from . import nlu
from .rule_agent import RuleAgent


class LLMAgent(RuleAgent):
    name = "llm"

    def understand(self, text: str, s: TripState) -> nlu.ParseResult:
        # TODO(LLM): вызвать GigaChat, распарсить JSON в nlu.ParseResult; при ошибке — правила.
        return super().understand(text, s)

    def phrase(self, draft: str, s: TripState) -> str:
        # TODO(LLM): переписать draft живым языком без изменения фактов.
        return draft
