"""规划编排器：把各模块串成一条完整的规划链路。

需求解析 → POI 检索 → 候选筛选 → 排程优化 → 路线评审 → 时间冲突检查 → 贴士增强。
每个阶段都通过 stage 回调上报，界面据此展示真实的深度思考进度。
对外只暴露 plan()，返回可直接落库的结构化行程。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from ..db import connection
from .requirements import parse_need
from .geo import get_provider
from .meals import enrich_meals
from .scheduler import build
from .timing import classify_all
from .verifier import verify
from .llm import get_llm

_MAX_TIPS = 12          # 单次规划最多生成多少条贴士，避免请求过多
_TIP_WORKERS = 4        # 并发请求数


def plan(need: str,
         tastes: Optional[List[str]] = None,
         destination: Optional[str] = None,
         start_date: Optional[str] = None,
         days: Optional[int] = None,
         interests: Optional[List[str]] = None,
         avoid: Optional[List[str]] = None,
         stage=None) -> dict:
    def notify(key: str) -> None:
        if stage is not None:
            stage(key)

    notify("weather_lookup")
    req = parse_need(need, tastes)
    if destination:
        req.destination = destination
    if start_date:
        req.start_date = start_date
    if days:
        req.days = days
    if interests:
        # 对话层已经给出的兴趣优先于本地词表
        req.interests = list(dict.fromkeys(list(interests) + list(req.interests)))
    req.avoid = list(avoid or [])

    if not req.destination:
        raise ValueError("未能识别目的地，请在需求里写明城市（如「南京」「成都」）")

    notify("attraction_search")
    candidates = get_provider().search(req.destination, req.interests)
    if not candidates:
        raise ValueError(f"没查到「{req.destination}」的景点，换个城市名试试（如「厦门」「西安」）")

    notify("candidate_builder")
    if req.avoid:
        # 负向约束是硬约束：明确说了不要的，不要出现在候选里
        blocked = [str(x) for x in req.avoid if str(x).strip()]
        kept = [c for c in candidates
                if not any(word in str(c.get("name") or "") or
                           word in str(c.get("category") or "") for word in blocked)]
        if len(kept) >= 4:
            candidates = kept

    notify("time_window")
    # 先判断每个地方「一天里最好什么时候去」，排程才知道该早出门还是该等傍晚
    classify_all(candidates, req.destination or "")

    notify("optimizer")
    plan_days = build(candidates, req.days, req.interests, req.pace, req.start_date)

    notify("reviewer")
    review_issues, repaired_days = _review_and_repair(plan_days)

    notify("meal_search")
    # 餐食不是占位：换成前后两站之间、带本地特色的真实餐馆，并补一顿晚餐
    enrich_meals(plan_days, req.destination or "")

    notify("time_check")
    warnings = verify(plan_days, req) + review_issues

    notify("finalize")
    _enrich_tips(plan_days, req.destination or "")

    return {
        "destination": req.destination,
        "days_count": len(plan_days),
        "days": plan_days,
        "warnings": warnings,
        "review": {
            "issues": review_issues,
            "repaired_days": repaired_days,
        },
    }


def _review_and_repair(plan_days: List[dict]):
    """路线评审：先做确定性一致性检查，能修的就地重排，修不了留给 warnings。"""
    from . import revision

    issues = revision.review({"days": plan_days})
    if not issues:
        return [], []

    broken: set = set()
    for issue in issues:
        if "时间倒流" in issue or "没有任何景点" in issue:
            matched = re.match(r"第(\d+)天", issue)
            if matched:
                broken.add(int(matched.group(1)))
    repaired: List[int] = []
    if broken:
        from .reviser import _is_meal
        from .scheduler import _optimize_day

        for day in plan_days:
            number = int(day.get("index") or 0)
            if number not in broken:
                continue
            spots = [s for s in (day.get("spots") or []) if not _is_meal(s)]
            if not spots:
                continue
            day["spots"] = _optimize_day(spots)
            repaired.append(number)
    remaining = revision.review({"days": plan_days}) if repaired else issues
    return remaining, repaired


# ── LLM 贴士增强（本地缓存 + 并发，避免拖慢规划）────────
# 贴士必须贴合「实际到访时段」：同一个景点排在上午和排在傍晚，该看的东西不一样。
# 所以缓存键带上时段，避免把「傍晚亮灯后最出片」复用给早上 9 点的行程。
_EVENING_WORDS = re.compile(r"傍晚|夜景|夜游|夜色|日落|落日|夕阳|黄昏|亮灯|入夜|晚上|夜晚|夜市|灯光")

_FALLBACK_TIPS: Dict[str, str] = {
    "morning": "上午到访最合适，人少、光线柔和，慢慢逛不用赶",
    "day": "白天来最合适，室内场馆不受天气影响",
    "afternoon": "下午到访刚好，逛完顺势接上晚饭",
    "dusk": "天色将暗时来最好看，灯光与轮廓都出来了",
    "night": "晚上来才值，灯亮之后的夜景才是主角",
}


def _minutes(value) -> Optional[int]:
    try:
        hour, minute = str(value or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def _tip_key(name: str, period: str) -> str:
    return f"{name}|{period or 'day'}"


def _cached_tips(keys: List[str]) -> Dict[str, str]:
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    try:
        with connection() as c:
            rows = c.execute(
                f"SELECT name, tip FROM spot_tips WHERE name IN ({marks})", tuple(keys)
            ).fetchall()
    except Exception:  # noqa: BLE001  旧库没有该表时静默跳过
        return {}
    return {r["name"]: r["tip"] for r in rows if r["tip"]}


def _store_tips(pairs: List[tuple]) -> None:
    if not pairs:
        return
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    try:
        with connection() as c:
            c.executemany(
                "INSERT INTO spot_tips(name, tip, created_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET tip=excluded.tip",
                [(name, tip, now) for name, tip in pairs],
            )
    except Exception:  # noqa: BLE001
        pass


def _ask_tip(llm, destination: str, spot: dict, slot: str) -> str:
    cat = spot.get("category") or "景点"
    prompt = (
        f"目的地「{destination}」，景点「{spot['name']}」（{cat}）。"
        f"行程把它安排在{slot}。"
        "用一句中文给出这个时段最值得看的东西、或最需要注意的一点，不超过30字，"
        "不要引号，不要重复景点名。"
        "必须贴合上面给出的到访时段：如果安排在上午，就不要写「傍晚亮灯后最佳」"
        "「日落时分最美」这类与时间相矛盾的建议；夜晚到访才可以说夜景、灯光。"
    )
    return llm.complete(prompt, max_tokens=72).strip()


def _tip_conflicts(tip: str, arrive: Optional[int]) -> bool:
    """上午/白天到访却写夜景，就是硬矛盾，直接换掉。"""
    if not tip or arrive is None:
        return False
    if arrive >= 16 * 60 + 30:
        return False
    return bool(_EVENING_WORDS.search(tip))


def _slot_text(day_no: int, seq: int, spot: dict) -> str:
    arrive, leave = spot.get("arrive"), spot.get("leave")
    mins = None
    a, b = _minutes(arrive), _minutes(leave)
    if a is not None and b is not None and b > a:
        mins = b - a
    span = f"{arrive} 到 {leave}" if arrive and leave else "当天"
    stay = f"（停留约 {mins} 分钟）" if mins else ""
    return f"第 {day_no} 天 {span}{stay}，当天第 {seq} 站"


def _enrich_tips(plan_days: List[dict], destination: str) -> None:
    llm = get_llm()
    if not llm:
        return
    targets: List[tuple] = []          # (spot, slot_text, period)
    for day in plan_days:
        day_no = int(day.get("index") or 0)
        seq = 0
        for spot in day.get("spots") or []:
            if spot.get("source") not in ("builtin", "amap") or not spot.get("name"):
                continue
            seq += 1
            targets.append((spot, _slot_text(day_no, seq, spot), spot.get("period") or "day"))
            if len(targets) >= _MAX_TIPS:
                break
        if len(targets) >= _MAX_TIPS:
            break
    if not targets:
        return

    keys = {id(spot): _tip_key(spot["name"], period) for spot, _slot, period in targets}
    cached = _cached_tips(list(set(keys.values())))
    for spot, _slot, period in targets:
        hit = cached.get(keys[id(spot)])
        if hit and not _tip_conflicts(hit, _minutes(spot.get("arrive"))):
            spot["tip"] = hit

    todo = [(s, slot, p) for s, slot, p in targets if not s.get("tip")]
    if not todo:
        return

    fresh: List[tuple] = []
    with ThreadPoolExecutor(max_workers=_TIP_WORKERS) as pool:
        futures = {
            pool.submit(_ask_tip, llm, destination, spot, slot): (spot, period)
            for spot, slot, period in todo
        }
        for future in as_completed(futures):
            spot, period = futures[future]
            try:
                tip = future.result()
            except Exception:  # noqa: BLE001  单条失败不影响整份行程
                continue
            if not tip:
                continue
            if _tip_conflicts(tip, _minutes(spot.get("arrive"))):
                # 模型没听话：按实际时段给一句稳妥的，不把矛盾留给用户
                tip = _FALLBACK_TIPS.get(period) or ""
                if not tip:
                    continue
            spot["tip"] = tip
            fresh.append((keys[id(spot)], tip))
    _store_tips(fresh)

    # 兜底：任何没拿到贴士或时段对不上的，用时段本身兜一句话
    for spot, _slot, period in targets:
        if not spot.get("tip") or _tip_conflicts(spot["tip"], _minutes(spot.get("arrive"))):
            fallback = _FALLBACK_TIPS.get(period)
            if fallback:
                spot["tip"] = fallback
