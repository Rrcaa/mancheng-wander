"""需求解析：把一句话旅行想法抽成结构化约束。

与原项目不同的点：用轻量规则解析而非 ReAct Agent，零依赖即可跑；
LLM 仅作为可选增强（见 llm 模块）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from ..sample_data import known_cities

_INTEREST_KEYWORDS = {
    "历史": ["历史", "博物馆", "古迹", "文物", "遗址", "古镇", "朝代", "博物院",
             "老建筑", "老街", "骑楼", "老洋房", "人文", "古城", "旧城"],
    # 注意别用单独的「海」字，否则「上海」会被判成海滨
    "自然": ["自然", "山水", "风景", "公园", "湖", "森林", "湿地", "山",
             "海边", "看海", "海岛", "沙滩", "海岸", "潜水", "冲浪", "海洋", "爬山"],
    "美食": ["美食", "吃", "小吃", "餐厅", "夜市", "火锅", "地道", "早茶", "咖啡", "米其林"],
    "现代": ["现代", "购物", "商圈", "夜景", "地标", "天际线", "时尚", "逛街", "看展"],
    "亲子": ["亲子", "带娃", "儿童", "动物园", "乐园", "科普"],
    "宗教": ["寺庙", "教堂", "宗教", "佛", "道观", "清真", "庵"],
}


@dataclass
class Requirement:
    destination: str | None
    days: int = 2
    start_date: str | None = None
    interests: List[str] = field(default_factory=list)
    avoid: List[str] = field(default_factory=list)
    pace: str = "balanced"  # relaxed | balanced | tight
    raw: str = ""


_CN_NUM = {"两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 从长句里捞目的地的粗略模式：「去厦门」「厦门 3 天」「到成都出差」
_CITY_PATTERNS = (
    r"(?:想去|准备去|计划去|出发去|去|到|在|游|玩转)\s*([\u4e00-\u9fa5]{2,6})",
    r"([\u4e00-\u9fa5]{2,4})\s*(?:\d+\s*(?:天|日|晚)|周末|出差|旅行|旅游|自由行)",
)

_NOT_CITY = {"旅游", "旅行", "出差", "周末", "自由行", "这次", "下次", "国内", "国外"}


def _guess_destination(text: str) -> str | None:
    """内置城市表没命中时，用「去X」这类句式 + 高德行政区校验认城市。"""
    from .geo import is_city

    tried: set = set()
    for pattern in _CITY_PATTERNS:
        for match in re.finditer(pattern, text):
            chunk = match.group(1)
            for size in range(min(4, len(chunk)), 1, -1):
                cand = chunk[:size]
                if cand in tried or cand in _NOT_CITY:
                    continue
                tried.add(cand)
                if is_city(cand):
                    return cand
    return None


def _llm_destination(text: str) -> str | None:
    """最后兜底：让模型只吐一个城市名。"""
    from .llm import get_llm

    llm = get_llm()
    if not llm:
        return None
    try:
        answer = llm.complete(
            f"从这句话里提取旅行目的地城市名，只输出城市名（2-4 个字），没有就输出「无」：{text}",
            max_tokens=16,
        )
    except Exception:  # noqa: BLE001
        return None
    name = (answer or "").strip().strip("。，,.!?\"'“”「」")
    if not name or name == "无" or len(name) > 6 or name in _NOT_CITY:
        return None
    return name


def _extract_date(text: str):
    """返回 (start_date_str, 去除了日期片段的文本)。"""
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if m:
        return (f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                (m.start(), m.end()))
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
        return (f"待定-{int(m.group(1)):02d}-{int(m.group(2)):02d}",
                (m.start(), m.end()))
    return (None, None)


def parse_need(text: str, taste_values: List[str] | None = None) -> Requirement:
    destination = next((c for c in known_cities() if c in text), None)
    if not destination:
        destination = _guess_destination(text)
    if not destination:
        destination = _llm_destination(text)

    start_date, span = _extract_date(text)
    # 先抠掉日期片段，避免「16日」被误判成天数
    work = text
    if span:
        work = work[:span[0]] + " " + work[span[1]:]

    m = re.search(r"(\d+|两|二|三|四|五|六|七|八|九|十)\s*(天|日|晚|day)", work, re.IGNORECASE)
    if m:
        tok = m.group(1)
        days = int(tok) if tok.isdigit() else _CN_NUM.get(tok, 2)
    else:
        days = 2

    interests: List[str] = []
    for cat, words in _INTEREST_KEYWORDS.items():
        if any(w in text for w in words):
            interests.append(cat)
    for v in taste_values or []:
        for cat, words in _INTEREST_KEYWORDS.items():
            if v in words and cat not in interests:
                interests.append(cat)

    pace = "balanced"
    if any(w in text for w in ["满满", "紧凑", "赶", "高效", "一天多"]):
        pace = "tight"
    elif any(w in text for w in ["轻松", "慢", "别太满", "悠闲", "放松", "不要太累"]):
        pace = "relaxed"

    return Requirement(destination=destination, days=days, start_date=start_date,
                       interests=interests, pace=pace, raw=text)
