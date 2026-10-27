"""大模型供应商（可选增强）。

不配置 Key 时返回 None，引擎退化为模板贴士；配置后用于生成更自然的景点贴士
与行程小结。接口与具体厂商解耦，后续可加通义/OpenAI 等。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from ..config import (
    DEEPSEEK_BASE,
    DEEPSEEK_KEY,
    DEEPSEEK_MODEL,
    DOUBAO_KEY,
    LLM_PROVIDER,
)

_TIMEOUT = httpx.Timeout(12.0, connect=5.0)


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, prompt: str, max_tokens: int = 120) -> str:
        ...

    def complete_json(self, system: str, prompt: str,
                      max_tokens: int = 700, temperature: float = 0.1) -> str:
        """结构化输出通道。默认退化为普通补全，具体厂商可覆盖。"""
        return self.complete(prompt, max_tokens=max_tokens)


def _clean(text: str) -> str:
    return (text or "").strip().strip('"“”「」').strip()


class DeepSeekProvider(LLMProvider):
    """DeepSeek 走 OpenAI 兼容协议。"""

    name = "deepseek"

    def complete(self, prompt: str, max_tokens: int = 120) -> str:
        return self._post(
            [{"role": "system", "content": "你是中文旅行规划助手，回答简洁、具体、不夸张。"},
             {"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=0.6, json_mode=False,
        )

    def complete_json(self, system: str, prompt: str,
                      max_tokens: int = 700, temperature: float = 0.1) -> str:
        """强制 JSON 模式：DeepSeek 支持 response_format=json_object。"""
        return self._post(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature, json_mode=True,
        )

    def _post(self, messages, *, max_tokens: int, temperature: float,
              json_mode: bool) -> str:
        if not DEEPSEEK_KEY:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY")
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                f"{DEEPSEEK_BASE.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                json=payload,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"DeepSeek {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek 返回为空")
        raw = choices[0].get("message", {}).get("content") or ""
        return raw.strip() if json_mode else _clean(raw)


def get_llm() -> Optional[LLMProvider]:
    if LLM_PROVIDER == "deepseek" and DEEPSEEK_KEY:
        return DeepSeekProvider()
    if LLM_PROVIDER == "doubao" and DOUBAO_KEY:
        # 豆包兼容 OpenAI 协议，可在此复用 DeepSeekProvider 的逻辑
        return None
    return None


def llm_status() -> dict:
    llm = get_llm()
    return {
        "provider": LLM_PROVIDER if llm else "",
        "model": DEEPSEEK_MODEL if llm else "",
        "ready": bool(llm),
    }
