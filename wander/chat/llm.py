"""LLM 工厂：DeepSeek 走 OpenAI 兼容协议。"""
from __future__ import annotations

import os
from typing import Any, Optional

from langchain_openai import ChatOpenAI

from ..config import DEEPSEEK_BASE, DEEPSEEK_KEY, DEEPSEEK_MODEL


def _missing() -> bool:
    return not DEEPSEEK_KEY


def build_chat_llm(*, temperature: float = 0.2) -> Optional[Any]:
    if _missing():
        return None
    return ChatOpenAI(
        model=os.getenv("MAIN_AGENT_MODEL") or DEEPSEEK_MODEL,
        api_key=DEEPSEEK_KEY,
        base_url=DEEPSEEK_BASE.rstrip("/"),
        temperature=temperature,
        timeout=60,
        max_retries=1,
    )


def build_dialogue_llm() -> Optional[Any]:
    """decision 兼容模式用的低温模型。"""
    if _missing():
        return None
    return ChatOpenAI(
        model=os.getenv("MAIN_AGENT_MODEL") or DEEPSEEK_MODEL,
        api_key=DEEPSEEK_KEY,
        base_url=DEEPSEEK_BASE.rstrip("/"),
        temperature=0,
        timeout=60,
        max_retries=1,
    )


def llm_ready() -> bool:
    return not _missing()
