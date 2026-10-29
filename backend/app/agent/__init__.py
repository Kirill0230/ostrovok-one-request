import os

from .rule_agent import RuleAgent


def get_agent():
    if os.getenv("AGENT", "rules") == "llm":
        from .llm_agent import LLMAgent
        return LLMAgent()
    return RuleAgent()
