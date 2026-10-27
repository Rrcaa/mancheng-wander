"""Chat Run 的 Agent 执行器：驱动 ReAct 主 Agent 并把过程翻译成 Runtime 事件。

职责边界：
- 只负责「跑图 + 转发事件 + 处理中断」，不做任何业务状态变更；
- 业务动作全部发生在工具里（tools.py / tool_service.py）；
- 中断（interrupt）交给 Runtime 记录成 `waiting_user`，恢复时用 `Command(resume=...)`。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


class AgentRunner:
    """每个进程一份 checkpointer；同一 Run 的恢复依赖 thread_id 不变。"""

    def __init__(self, bridge: Any):
        self.bridge = bridge
        self.saver = MemorySaver()
        self._graph = None

    def graph(self):
        if self._graph is None:
            from .react_graph import build_main_agent_graph
            from .tool_service import MainAgentToolService
            from .tools import build_main_agent_tools

            tools = build_main_agent_tools(MainAgentToolService(self.bridge))
            self._graph = build_main_agent_graph(tools, checkpointer=self.saver)
        return self._graph

    @staticmethod
    def thread_config(run: dict) -> dict:
        return {"configurable": {
            "thread_id": f"chat:{run['conversation_id']}:{run['id']}",
        }}

    async def run_turn(
        self,
        run: dict,
        *,
        messages: Optional[list[Any]] = None,
        resume_value: Optional[Any] = None,
        on_custom=None,
        on_delta=None,
    ) -> dict[str, Any]:
        """跑一轮（或恢复一轮），返回 {interrupted, interaction, final_text, messages}。"""
        graph = self.graph()
        config = self.thread_config(run)

        if resume_value is not None:
            graph_input: Any = Command(resume=resume_value)
        else:
            graph_input = {"messages": messages or []}

        context = self.bridge.agent_context(run)
        interrupted: Optional[dict] = None
        final_text = ""
        collected: list[Any] = []
        model_node = "model"

        async for part in graph.astream(
            graph_input,
            config=config,
            context=context,
            stream_mode=["custom", "messages", "values"],
            version="v2",
            durability="sync",
        ):
            kind = part.get("type") if isinstance(part, dict) else None
            data = part.get("data") if isinstance(part, dict) else None

            if kind == "custom" and isinstance(data, dict):
                if on_custom:
                    on_custom(data)
                continue

            if kind == "messages" and isinstance(data, tuple) and len(data) == 2:
                chunk, meta = data
                if (meta or {}).get("langgraph_node") != model_node:
                    continue
                delta = _text_of(chunk)
                if delta and on_delta:
                    on_delta(delta)
                continue

            if kind == "values" and isinstance(data, dict):
                if data.get("__interrupt__"):
                    interrupted = self._interaction(data["__interrupt__"])
                msgs = data.get("messages")
                if isinstance(msgs, list):
                    collected = msgs

        if interrupted is None:
            try:
                state = await graph.aget_state(config)
                pending = getattr(state, "interrupts", None) or ()
                if pending:
                    interrupted = self._interaction(pending)
            except Exception:  # noqa: BLE001
                pass

        ai_texts = [
            _text_of(message) for message in collected
            if getattr(message, "type", None) == "ai" and not getattr(message, "tool_calls", None)
        ]
        final_text = next((text for text in reversed(ai_texts) if text.strip()), "")

        return {
            "interrupted": interrupted is not None,
            "interaction": interrupted,
            "final_text": final_text,
            "messages": collected,
        }

    @staticmethod
    def _interaction(interrupts: Any) -> dict:
        first = interrupts[0] if isinstance(interrupts, (list, tuple)) and interrupts else interrupts
        value = getattr(first, "value", None) or {}
        if not isinstance(value, dict):
            value = {"question": str(value)}
        interaction_id = str(
            value.get("interaction_id") or getattr(first, "id", "") or ""
        )
        return {
            "interaction_id": interaction_id,
            "question": str(value.get("question") or "请补充必要信息。"),
            "missing_fields": list(value.get("missing_fields") or []),
            "input_schema": value.get("input_schema") or {"type": "string", "minLength": 1},
        }


def collect_artifacts(messages: list[Any]) -> list[dict]:
    """从工具消息里收集卡片产物（例如历史方案列表）。"""
    out: list[dict] = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, dict):
            out.append(artifact)
    seen, unique = set(), []
    for item in out:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:5]
