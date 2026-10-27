"""Dialogue Agent —— 自然语言理解层。

三条硬规则：
1. LLM 是唯一理解来源：意图、字段、目标引用、追问全部由结构化模型产出，
   这里不使用关键词表、正则或固定句式做意图分类与字段抽取。
2. 严格结构化：结果必须通过 DialogueDecision（Pydantic）校验，未知 intent /
   未知动作 / 类型不符一律拒绝，并触发一次修复重试。
3. 失败无副作用：两次都失败时抛出 UnderstandingFailed，调用方不得改动
   PlanningBrief、不得创建 Revision、不得控制其他 Run。

本模块只做「理解」，不做任何业务动作——动作由 compat 里的确定性执行器消费
已校验的 DialogueDecision 完成。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .llm import get_llm

# ── 结构化契约 ──────────────────────────────────────
Intent = Literal[
    "travel_consultation",   # 旅行咨询：问季节、问值不值得去、问交通，不建单
    "chitchat",              # 普通聊天/寒暄
    "create_plan",           # 想规划（新行程）
    "update_brief",          # 补充或纠正正在收集的信息
    "confirm_plan",          # 明确要求开始正式规划
    "modify_itinerary",      # 改某份已存在的行程
    "run_control",           # 取消/重试正在跑的任务
    "unclear",               # 没听懂 / 目标不唯一，需要澄清
]

Pace = Literal["relaxed", "balanced", "tight"]

# 模型回复里出现这些说法时，视作「谎称已开始规划」
_FALSE_START = re.compile(r"(?:已经|正在|已)[^，。！？\n]{0,6}规划")


class BriefPatch(BaseModel):
    """对 PlanningBrief 的增量。没提到的字段留 None，执行器只做增量合并。"""
    destination: Optional[str] = None
    days: Optional[int] = Field(default=None, ge=1, le=30)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    pace: Optional[Pace] = None
    companions: Optional[str] = None
    transport: Optional[str] = None
    budget: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    avoid: List[str] = Field(default_factory=list)
    food: Optional[str] = None
    note: Optional[str] = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso_date(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        value = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("日期必须是 YYYY-MM-DD")
        return value

    @field_validator("destination", "companions", "transport", "budget", "food", "note")
    @classmethod
    def _short_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        return value[:40] or None


class DialogueDecision(BaseModel):
    intent: Intent
    reply: str = Field(min_length=1, max_length=400)
    brief_patch: BriefPatch = Field(default_factory=BriefPatch)
    target_itinerary_id: Optional[str] = None
    run_action: Optional[Literal["cancel", "retry"]] = None
    clarification: Optional[str] = None
    missing_fields: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)

    @field_validator("reply")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("reply 不能为空")
        return value

    def asserts_false_start(self) -> bool:
        """回复声称已开始规划，但结构化意图并非 confirm_plan —— 视为冲突。"""
        return self.intent != "confirm_plan" and bool(_FALSE_START.search(self.reply))


class UnderstandingFailed(RuntimeError):
    """两次结构化输出都无效。调用方必须保持零副作用并把 Run 标成可重试失败。"""


# ── 提示词 ──────────────────────────────────────────
_SYSTEM = """你是旅行产品里的对话理解器。你唯一的职责是：把用户这一句话，连同给定上下文，\
翻译成一份严格的 JSON 决策。你不负责规划行程，也不负责查景点。

必须返回 JSON 对象，字段固定为：
{
  "intent": "travel_consultation|chitchat|create_plan|update_brief|confirm_plan|modify_itinerary|run_control|unclear",
  "reply": "给用户看的中文回复（必填，1-3 句）",
  "brief_patch": {                    // 只在 intent 为 create_plan / update_brief 时填写增量
    "destination": "城市名或 null",
    "days": 整数或 null,
    "start_date": "YYYY-MM-DD 或 null",
    "end_date": "YYYY-MM-DD 或 null",
    "pace": "relaxed|balanced|tight 或 null",
    "companions": "同行人，如 带娃/情侣/独自/朋友/长辈 或 null",
    "transport": "交通方式，如 自驾/公共交通/高铁 或 null",
    "budget": "预算描述或 null",
    "interests": ["想玩的方向，如 历史/自然/美食/现代/亲子/宗教"],
    "avoid": ["明确不想去或不想安排的内容"],
    "food": "餐饮偏好或 null",
    "note": "其它约束或 null"
  },
  "target_itinerary_id": "要修改的行程 id 或 null",
  "run_action": "cancel|retry|null",
  "clarification": "需要澄清时的问题，或 null",
  "missing_fields": ["destination"],   // 正式规划还缺哪些必填项，取值只有 destination / days / taste
  "confidence": 0.0 到 1.0
}

判定规则（必须严格遵守）：
1. 用户只是问情况（季节、值不值得、交通、天气、注意事项）→ travel_consultation，\
直接回答，brief_patch 留空，绝不因为提到城市就建单。
2. 用户表达了出行意愿但信息不全 → create_plan（首次）或 update_brief（已有简报时补充/纠正），\
brief_patch 只写这次新出现的或需要纠正的字段，已确认且未被纠正的字段一律留 null。
3. 用户说「开始规划/就这样/确认」且简报已齐 → confirm_plan。
4. 用户要改某份具体行程 → modify_itinerary，并在 target_itinerary_id 填上下文里给到的行程 id。\
如果上下文里有不止一份行程、用户又没指明，intent 用 unclear 并在 reply 里请用户选。
5. 用户要取消或重试当前任务 → run_control + run_action。
6. 完全听不懂、或信息自相矛盾 → unclear。
7. reply 必须与结构化字段一致：除 confirm_plan 外，绝不说「已经开始规划」「正在规划」。
8. missing_fields 非空时，reply 只能问 missing_fields 里的第一项（destination 问城市，
   dates 问具体出发日期），不要跑去问预算、兴趣、同行之类的可选偏好。
9. 日期按上下文给出的今天推算；「明天」「下周末」「国庆」都要换算成具体 YYYY-MM-DD。
10. 用户提到天数就不要追问天数；用户提到目的地就不要追问目的地。
11. 不要编造景点、价格、营业时间。reply 不超过 60 字，口语化。
"""

_SCHEMA_HINT = ("只输出 JSON 对象本身，不要 markdown 代码块、不要解释文字。")


def _history_lines(history: List[Dict[str, str]], limit: int = 8) -> str:
    rows = [m for m in (history or []) if (m.get("content") or "").strip()][-limit:]
    out = []
    for m in rows:
        who = "用户" if m.get("role") == "user" else "助手"
        text = re.sub(r"\s+", " ", m["content"]).strip()[:160]
        out.append(f"{who}：{text}")
    return "\n".join(out) or "（无）"


def _brief_lines(brief: Optional[dict]) -> str:
    if not brief:
        return "（还没有简报）"
    data = brief.get("data") or {}
    bits = []
    for key, label in (("destination", "目的地"), ("days", "天数"),
                       ("start_date", "出发日期"), ("end_date", "结束日期"),
                       ("pace", "节奏"), ("companions", "同行"),
                       ("transport", "交通"), ("budget", "预算"),
                       ("attraction_preference", "偏好"), ("food", "餐饮"),
                       ("habit_preference", "习惯")):
        if data.get(key):
            bits.append(f"{label}={data[key]}")
    if data.get("trip_constraints"):
        bits.append("画像约束=" + "、".join(
            str(c.get("value_text")) for c in data["trip_constraints"] if c.get("value_text")))
    status = brief.get("status")
    missing = "、".join(brief.get("missing_fields") or []) or "无"
    return f"状态={status}；缺失={missing}；" + ("；".join(bits) or "字段全空")


def _itinerary_lines(itineraries: List[dict]) -> str:
    if not itineraries:
        return "（没有任何已存在的行程）"
    out = []
    for item in itineraries[:5]:
        out.append(
            f"- id={item.get('id')}；{item.get('destination') or '未知'}；"
            f"{item.get('days_count') or '?'} 天；出发 {item.get('start_date') or '未定'}；"
            f"标题「{item.get('title') or ''}」"
        )
    return "\n".join(out)


def build_prompt(context: Dict[str, Any]) -> str:
    return "\n".join([
        f"今天是 {context.get('today')}（{context.get('weekday')}），"
        f"当前时间 {context.get('now')}，时区 {context.get('timezone', 'Asia/Shanghai')}。",
        "",
        "【最近的对话】",
        _history_lines(context.get("history") or []),
        "",
        "【当前活动简报】",
        _brief_lines(context.get("brief")),
        "",
        "【用户已存在的行程】",
        _itinerary_lines(context.get("itineraries") or []),
        "",
        f"【本会话绑定的行程】{context.get('bound_itinerary_id') or '无'}",
        f"【正在跑的任务】{json.dumps(context.get('active_run'), ensure_ascii=False) if context.get('active_run') else '无'}",
        "",
        "【用户这句话】",
        (context.get("message") or "").strip(),
        "",
        _SCHEMA_HINT,
    ])


def _parse(raw: str) -> DialogueDecision:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型没有返回 JSON") from None
        payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    decision = DialogueDecision(**payload)
    if decision.asserts_false_start():
        raise ValueError("回复与结构化意图冲突：未确认却声称已开始规划")
    if decision.intent in ("create_plan", "update_brief"):
        patch = decision.brief_patch
        if not any([patch.destination, patch.days, patch.start_date, patch.end_date,
                    patch.pace, patch.companions, patch.transport, patch.budget,
                    patch.interests, patch.avoid, patch.food, patch.note]):
            raise ValueError("规划意图必须至少携带一个 brief 字段")
    # modify_itinerary 缺 target 不算错误：执行器会用会话绑定兜底，
    # 兜底也不唯一时再要求用户指认（权威校验在执行器，不在这里）
    return decision


def understand(context: Dict[str, Any],
               repair_hint: Optional[str] = None) -> DialogueDecision:
    """一次调用 + 最多一次结构化修复。失败抛 UnderstandingFailed。"""
    llm = get_llm()
    if llm is None:
        raise UnderstandingFailed("未配置大模型，无法理解这句话")

    prompt = build_prompt(context)
    if repair_hint:
        prompt += f"\n\n上一次输出不合格：{repair_hint}\n请重新给出完全符合 schema 的 JSON。"

    last_error = ""
    for attempt in range(2):
        try:
            raw = llm.complete_json(_SYSTEM, prompt, max_tokens=800, temperature=0.1)
            return _parse(raw)
        except (ValueError, ValidationError) as error:
            last_error = str(error)[:200]
            prompt = build_prompt(context) + (
                f"\n\n上一次输出不合格：{last_error}\n"
                "请严格按 schema 重新输出 JSON 对象，不要任何多余文字。"
            )
        except Exception as error:  # noqa: BLE001  网络/额度问题，不再重试
            raise UnderstandingFailed(f"模型调用失败：{error}") from error
    raise UnderstandingFailed(last_error or "结构化理解失败")
