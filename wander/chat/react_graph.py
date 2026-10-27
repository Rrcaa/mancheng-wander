"""ReAct 主 Agent 图工厂（LangChain create_agent）。"""
from __future__ import annotations

from typing import Any

from .llm import build_chat_llm
from .models import MainAgentContext
from .prompts import MAIN_AGENT_SYSTEM


def build_main_agent_graph(tools: list[Any], *, checkpointer: Any = None, llm: Any = None) -> Any:
    from langchain.agents import create_agent

    if llm is None:
        llm = build_chat_llm(temperature=0)
    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=MAIN_AGENT_SYSTEM,
        context_schema=MainAgentContext,
        checkpointer=checkpointer,
        name="wander_main_agent",
    )
