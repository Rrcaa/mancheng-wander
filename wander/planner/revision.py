"""行程修订：Planner / Reviewer 两段式。

修改不靠正则匹配句式，而是两段式：
Planner —— 让模型读「当前行程 + 用户这句话」，输出一份结构化的修改操作单；
Executor —— 确定性代码执行操作单（删、换、加、调节奏、移时间、加减天），
            被影响的那一天重新交给 CP-SAT 排顺序与时刻；
Reviewer —— 对结果做一致性检查（重复景点、时间倒流、空天、缺午餐），
            有问题就把问题清单回喂给 Planner 修一次。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import geo, optimizer
from .llm import get_llm
from .reviser import (_day_number, _is_meal, _recalc_dist, _retime_day,
                      _to_spot, _tidy, find_existing)

OPS = ("remove", "add", "replace", "pace", "shift", "resequence",
       "add_day", "remove_day", "avoid")

_SYSTEM = """你是行程修改的规划器。给定一份现有行程和用户的一句修改要求，\
你要输出一份精确的修改操作单（JSON），不要解释、不要寒暄、不要输出 JSON 之外的任何字符。

返回格式：
{
  "ops": [
    {"op": "remove",     "day": 2, "name": "故宫"},                  // day 为 null 表示在全部天里找
    {"op": "replace",    "day": 1, "from": "玄武湖", "to": "总统府"},
    {"op": "add",        "day": 2, "keyword": "美术馆"},              // 用关键词去搜一个真实景点
    {"op": "pace",       "day": null, "value": "relaxed|balanced|tight"},  // 放松/紧凑某天或全程
    {"op": "shift",      "day": 2, "minutes": 60},                    // 推迟 60 分钟，提前用负数
    {"op": "add_day",    "count": 1},
    {"op": "remove_day", "day": 3},
    {"op": "resequence", "day": 2},                                   // 只重排顺序，不增删
    {"op": "avoid",      "keyword": "爬山"}                            // 之后不要再安排这类
  ],
  "reply_hint": "一句话说明改了什么（中文，不超过 40 字）"
}

规则：
1. 只输出用户真的要求了的操作，不要顺手优化别的天。
2. 用户说「太赶/太满/松一点」用一个 pace；「想多待一会儿」用 shift。
3. 用户点名某一天（「第二天」「最后一天」「第一天」）时，对应操作的 day 必须是那一天的数字，
   不能填 null；只有用户说的是「整体/每天/全程」时 day 才填 null。
4. 用户点名某处要去掉/换成，from/name 必须原样使用行程里出现的名称；行程里没有的名称不要硬填。
5. 模型不认识景点时不要编造：用 add + keyword，让系统去搜。
6. day 用行程里的 index（1 开始）。
"""

_REVIEW_SYSTEM = """你是行程质检员。检查给定行程是否存在下列问题，只输出 JSON：
{"issues": ["问题描述", ...]}
没有问题时输出 {"issues": []}。
只检查：同一景点重复出现在同一天或相邻两天、同一天时间倒流或重叠、\
某天没有任何景点、某天缺少午餐、用户明确要求避开的内容仍然存在。
不要评价景点好不好，不要提改进建议。
"""


def _plan_digest(plan: dict) -> str:
    lines = []
    for day in plan.get("days") or []:
        index = _day_number(day, len(lines) + 1)
        spots = day.get("spots") or []
        names = ", ".join(
            f"{s.get('name')}({s.get('arrive') or '?'}-{s.get('leave') or '?'})"
            for s in spots if s.get("name")
        )
        lines.append(f"第{index}天（{day.get('date') or '未定'}，主题 {day.get('theme') or '-'}）：{names or '空'}")
    return "\n".join(lines) or "（行程为空）"


def _ask_ops(plan: dict, instruction: str, extra: str = "") -> Dict[str, Any]:
    llm = get_llm()
    if llm is None:
        raise RuntimeError("未配置大模型")
    prompt = (
        f"目的地：{plan.get('destination') or '未知'}\n"
        f"当前行程：\n{_plan_digest(plan)}\n\n"
        f"用户的修改要求：{instruction[:200]}\n"
        + (f"\n上一次输出的问题：{extra}\n" if extra else "")
    )
    raw = llm.complete_json(_SYSTEM, prompt, max_tokens=600, temperature=0.1)
    return _load_json(raw)


def _load_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有返回 JSON")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("返回的不是 JSON 对象")
    return payload


def _day_at(plan: dict, day_no) -> Optional[dict]:
    days = plan.get("days") or []
    try:
        no = int(day_no)
    except (TypeError, ValueError):
        return None
    for i, day in enumerate(days):
        if _day_number(day, i + 1) == no:
            return day
    return None


def _find(day: dict, name: str) -> Optional[int]:
    target = re.sub(r"\s+", "", str(name or ""))
    if not target:
        return None
    for i, spot in enumerate(day.get("spots") or []):
        cur = re.sub(r"\s+", "", str(spot.get("name") or ""))
        if cur and (target == cur or target in cur or cur in target):
            return i
    return None


def _search(city: str, keyword: str) -> Optional[dict]:
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    for poi in geo.text_search(city, keyword, "attraction", limit=6):
        if poi.get("location") or (poi.get("lon") and poi.get("lat")):
            return _to_spot(poi)
    return None


def _reseque(day: dict, city: str = "") -> None:
    spots = [s for s in (day.get("spots") or []) if not _is_meal(s)]
    if len(spots) < 2:
        return
    # 用户新加的地点还没有时段判断，先补上，否则夜景会被排到中午
    missing = [s for s in spots if not s.get("period")]
    if missing:
        try:
            from .timing import LABELS, classify_all, classify_rule
            if city:
                classify_all(missing, city)
            else:
                for spot in missing:
                    period = classify_rule(spot)
                    spot["period"] = period
                    spot["period_label"] = LABELS.get(period, "")
        except Exception:  # noqa: BLE001  时段判断失败不影响重排
            pass
    # 保留用户设定的出发时间：重排顺序不能把「晚点出发」冲掉；没设过就交给求解器按时段定
    starts = [_minutes(s.get("arrive")) for s in spots]
    earliest = min([x for x in starts if x is not None], default=None)
    ordered = optimizer.order(spots, start_min=earliest or None)
    if not ordered:
        return
    day["spots"] = list(ordered)


def apply_ops(plan: dict, ops: List[dict], city: str) -> Tuple[dict, List[str], List[str]]:
    """返回 (plan, 改动说明, 没落地的操作描述)。"""
    changes: List[str] = []
    unresolved: List[str] = []
    days = plan.get("days") or []
    touched: List[dict] = []

    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip()
        if kind not in OPS:
            continue
        day = _day_at(plan, op.get("day"))
        scope = days if (op.get("day") is None or day is None) else [day]

        if kind == "remove":
            name = op.get("name") or ""
            hit = False
            for target in scope:
                idx = _find(target, name)
                if idx is None:
                    continue
                dropped = target["spots"].pop(idx)
                _tidy(target)
                changes.append(f"第{_day_number(target, 0)}天去掉了「{dropped.get('name')}」")
                hit = True
                touched.append(target)
                break
            if not hit:
                unresolved.append(f"去「{name}」")

        elif kind == "replace":
            old, new = op.get("from") or "", op.get("to") or ""
            poi = _search(city, new)
            if not poi:
                unresolved.append(f"把「{old}」换成「{new}」（没搜到后者的位置）")
                continue
            hit = False
            for target in scope:
                idx = _find(target, old)
                if idx is None:
                    continue
                old_spot = target["spots"][idx]
                spot = dict(poi)
                spot["arrive"], spot["leave"] = old_spot.get("arrive"), old_spot.get("leave")
                target["spots"][idx] = spot
                _tidy(target)
                changes.append(f"第{_day_number(target, 0)}天把「{old_spot.get('name')}」换成了「{spot.get('name')}」")
                touched.append(target)
                hit = True
                break
            if not hit:
                unresolved.append(f"把「{old}」换成「{new}」（行程里没有「{old}」）")

        elif kind == "add":
            poi = _search(city, op.get("keyword") or op.get("name") or "")
            if not poi:
                unresolved.append(f"加上「{op.get('keyword') or ''}」（没搜到位置）")
                continue
            target = day or (days[-1] if days else None)
            if not target:
                continue
            existing = find_existing(days, poi)
            if existing:
                # 同一处地点已经在行程里了，再 append 一条就会变成两张一样的卡
                unresolved.append(f"「{existing[1]}」第{existing[0]}天已经安排了")
                continue
            target.setdefault("spots", []).append(poi)
            changes.append(f"第{_day_number(target, 0)}天加上了「{poi.get('name')}」")
            touched.append(target)

        elif kind == "pace":
            value = op.get("value")
            if value == "relaxed":
                trimmed = 0
                for target in scope:
                    attractions = [s for s in target.get("spots") or [] if not _is_meal(s)]
                    if len(attractions) > 2:
                        target["spots"].remove(attractions[-1])
                        _tidy(target)
                        touched.append(target)
                        trimmed += 1
                if trimmed:
                    scope_text = "每天" if len(scope) > 1 else f"第{_day_number(scope[0], 0)}天"
                    changes.append(f"{scope_text}松开了，少安排一处")
            elif value == "tight":
                for target in scope:
                    _missing_meal(target)
                changes.append("节奏调得更紧凑")
                touched.extend(scope)

        elif kind == "shift":
            try:
                minutes = int(op.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes:
                for target in scope:
                    for spot in target.get("spots") or []:
                        for key in ("arrive", "leave"):
                            if spot.get(key):
                                spot[key] = _shift_hm(spot[key], minutes)
                    touched.append(target)
                verb = "推迟" if minutes > 0 else "提前"
                changes.append(f"{verb}了 {abs(minutes) // 60 or 1} 小时")

        elif kind == "resequence":
            for target in scope:
                _reseque(target, city)
                touched.append(target)
            changes.append("重排了当天的游览顺序，路程更短")

        elif kind == "add_day":
            try:
                count = max(1, min(5, int(op.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1
            for _ in range(count):
                days.append({"index": len(days) + 1, "date": None,
                             "theme": "自由活动", "spots": []})
            changes.append(f"多了 {count} 天")

        elif kind == "remove_day":
            if len(days) <= 1:
                return plan, [], ["只剩一天了，不能再减"]
            target = day or days[-1]
            gone = target.get("index")
            days.remove(target)
            changes.append(f"去掉了第 {gone} 天")

        elif kind == "avoid":
            keyword = str(op.get("keyword") or "")
            if keyword:
                trimmed = 0
                for target in days:
                    for spot in list(target.get("spots") or []):
                        if _is_meal(spot):
                            continue
                        if keyword in str(spot.get("name") or "") or keyword in str(spot.get("category") or ""):
                            target["spots"].remove(spot)
                            trimmed += 1
                for target in days:
                    _tidy(target)
                if trimmed:
                    changes.append(f"去掉了 {trimmed} 处「{keyword}」")

    for target in {id(t): t for t in touched}.values():
        _reseque(target, city)
        _recalc_dist(target.get("spots") or [])
    for i, target in enumerate(days):
        target["index"] = i + 1
    # 景点换过之后，午餐/晚餐要按新的上下站重新挑一次，否则会停在离谱的位置
    try:
        from .meals import enrich_meals
        enrich_meals(days, plan.get("destination") or "")
    except Exception:  # noqa: BLE001  餐食重排失败不影响其余修改
        pass
    return plan, changes, unresolved


def _shift_hm(value: str, minutes: int) -> str:
    try:
        hour, minute = str(value).split(":")[:2]
        total = int(hour) * 60 + int(minute) + minutes
    except (TypeError, ValueError):
        return value
    total = max(0, min(24 * 60 - 1, total))
    return f"{total // 60:02d}:{total % 60:02d}"


def _missing_meal(day: dict) -> None:
    if any(_is_meal(s) for s in day.get("spots") or []):
        return
    day.setdefault("spots", []).append({
        "name": "午餐", "category": "美食", "type": "lunch",
        "address": None, "lon": None, "lat": None,
        "arrive": "12:00", "leave": "13:00",
        "tip": "就近用餐，顺便歇脚", "source": "meal",
    })
    _retime_day(day["spots"])


def review(plan: dict) -> List[str]:
    """确定性一致性检查；返回问题清单（空表示通过）。"""
    issues: List[str] = []
    seen: Dict[str, int] = {}
    for day in plan.get("days") or []:
        no = _day_number(day, 0)
        spots = day.get("spots") or []
        attractions = [s for s in spots if not _is_meal(s)]
        if not attractions:
            issues.append(f"第{no}天没有任何景点")
        last = -1
        for spot in spots:
            start = _minutes(spot.get("arrive"))
            end = _minutes(spot.get("leave"))
            if start is not None and start < last:
                issues.append(f"第{no}天「{spot.get('name')}」时间倒流")
                break
            if start is not None and end is not None:
                last = end
        names = [str(s.get("name") or "") for s in attractions]
        for name in names:
            if not name:
                continue
            if name in seen:
                issues.append(f"「{name}」在第{seen[name]}天和第{no}天重复出现")
            else:
                seen[name] = no
    return issues


def _minutes(value) -> Optional[int]:
    try:
        hour, minute = str(value or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def revise(plan: dict, instruction: str, city: str = "") -> Tuple[dict, List[str], Optional[dict]]:
    """返回 (修改后的行程, 改动说明, 需要澄清的交互描述)。"""
    city = city or plan.get("destination") or ""
    try:
        payload = _ask_ops(plan, instruction)
    except Exception as error:  # noqa: BLE001
        return plan, [], {
            "question": f"这句我没理解成功，能换个说法吗？（{str(error)[:40]}）",
            "missing_fields": ["instruction"],
        }

    ops = payload.get("ops") or []
    if not ops:
        hint = str(payload.get("reply_hint") or "").strip()
        return plan, [], {
            "question": hint or "想调整哪一天、哪一处？可以说「第二天去掉某某」「把 A 换成 B」。",
            "missing_fields": ["instruction"],
        }

    plan, changes, unresolved = apply_ops(plan, ops, city)
    issues = review(plan)
    if issues:
        # Reviewer 打回 → 让 Planner 带着问题清单再改一次
        try:
            retry = _ask_ops(plan, instruction, extra="；".join(issues[:4]))
            retry_ops = retry.get("ops") or []
            if retry_ops:
                plan, extra_changes, extra_unresolved = apply_ops(plan, retry_ops, city)
                changes.extend(extra_changes)
                unresolved.extend(extra_unresolved)
                issues = review(plan)
        except Exception:  # noqa: BLE001
            pass

    if not changes:
        return plan, [], {"question": _unresolved_question(plan, unresolved) or
                          "这句没匹配到行程里的具体位置，能说具体点吗？",
                          "missing_fields": ["instruction"]}
    return plan, changes, None


def _unresolved_question(plan: dict, unresolved: List[str]) -> Optional[str]:
    """操作没落地时，把当天真实排的内容摆出来，请用户指认。"""
    if not unresolved:
        return None
    first = unresolved[0]
    day_no = None
    m = re.search(r"第(\d)天", first + " ")
    for day in plan.get("days") or []:
        if m and str(_day_number(day, 0)) == m.group(1):
            day_no = day
            break
    if day_no is None:
        return f"想{first}，但这处和当前行程对不上。要不要说个具体景点名？"
    names = [s.get("name") for s in day_no.get("spots") or [] if not _is_meal(s)]
    if not names:
        return f"想{first}，但这天还没排景点。"
    return f"想{first}，可第{_day_number(day_no, 0)}天现在排的是「{'」「'.join(names[:4])}」，要动哪一个？"
