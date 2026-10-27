"""排程编排：选什么 + 分到哪天由这里决定，单日顺序与时刻交给 OR-Tools 求解器。

选点不是「评分最高的几个」，而是三个因素一起权衡：
  1. 距离——同片区优先，一天不来回跨城；
  2. 时段——挑进来的景点要能连成一条时间线（上午→白天→傍晚→夜晚），
     不能出现「1 个上午 + 2 个夜里」这种中间空半天的组合；
  3. 时长预算——一天塞不下就留给第二天，别把 4 个各 3 小时的乐园堆在一起。
单日内部的顺序与每站几点到，交给 optimizer.order() 的 CP-SAT 求最优回路。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta
from typing import Dict, List

from . import optimizer
from .timing import period_index

_SPOTS_PER_DAY = {"relaxed": 2, "balanced": 3, "tight": 4}
# 一天的景点数上限：按节奏给出的人数之外，还允许再补两处把傍晚填满，
# 但最多 5 处——再多就成赶场了（verifier 也会提示节奏偏赶）
_PER_DAY_CEILING = 5
# 一天的游览时长上限（不含吃饭与赶路）；超了宁愿留给第二天
_DAY_BUDGET = {"relaxed": 6 * 60, "balanced": 8 * 60, "tight": 9 * 60 + 30}
# 全天都是傍晚/夜景的那条线，可用窗口只有 17:00–23:00，装不下白天那份预算。
# 240 分钟 ≈ 三处停留，正好是「看日落 → 看夜景 → 吃夜宵」的量
_NIGHT_BUDGET = 4 * 60
_BUDGET_SLACK = 90          # 允许略微超一点，避免一个景点都排不进去
_VISIT_DEFAULT = 90
_TRANSIT_BUFFER = 15  # 景点间路程缓冲（分钟）

_MAX_PERIOD_GAP = 2         # 同一天里相邻景点的时段最多差两档（上午→下午尚可，上午→夜晚不行）
_GAP_COST = 40.0            # 跨度过大的代价：相当于「除非没得选，否则不要」
_NEAR_GAP_COST = 1.5        # 时段相邻但不重叠，轻微扣分
_CAT_COST = 2.5             # 类别重复，避免一天全是同一类
_OVER_BUDGET_COST = 3.0
_FAR_OVER_BUDGET_COST = 25.0
_SCORE_KM = 1.4             # 评分 1 分 ≈ 1.4km 路程的权重



def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _score(poi: dict, interests: List[str]) -> float:
    s = float(poi.get("rating", 4.0))
    if interests:
        s += sum(0.6 for t in poi.get("tags", []) if t in interests)
    return s


def _visit_of(spot: dict) -> int:
    try:
        value = int(spot.get("visit") or _VISIT_DEFAULT)
    except (TypeError, ValueError):
        value = _VISIT_DEFAULT
    return max(30, min(240, value))


def _theme(spots: List[dict]) -> str | None:
    if not spots:
        return None
    return Counter(s["category"] for s in spots).most_common(1)[0][0]


def build(candidates: List[dict], days: int, interests: List[str],
          pace: str, start_date: str | None = None) -> List[dict]:
    per = _SPOTS_PER_DAY.get(pace, 3)
    ceiling = min(per + 2, _PER_DAY_CEILING)
    budget = _DAY_BUDGET.get(pace, 8 * 60)
    pool = sorted(candidates, key=lambda p: -_score(p, interests))
    areas = _cluster_areas(pool)          # id(poi) -> 片区编号（按坐标聚类）
    used: set = set()
    plan: List[dict] = []

    for d in range(days):
        # 选种子：分数最高的未用景点
        seed = next((p for p in pool if id(p) not in used), None)
        if seed is None:
            break
        day_spots: List[dict] = [seed]
        used.add(id(seed))

        # 先把同片区的景点凑一起（如鼓浪屿不被拆到两天），允许比常规多两个，
        # 但游览时长不能超过当天预算，时段也不能拉太开——否则「夜游线」里会混进
        # 一个白天类的同片区景点，那天就空出整整一个下午
        area = areas.get(id(seed))
        if area is not None:
            same = [p for p in pool if id(p) not in used and areas.get(id(p)) == area]
            for p in sorted(same, key=_near_key([seed])):
                if len(day_spots) >= ceiling:
                    break
                if _minutes_of(day_spots) + _visit_of(p) > _budget_for(day_spots, budget) + _BUDGET_SLACK:
                    continue
                if _period_gap(day_spots, p) > _MAX_PERIOD_GAP:
                    continue
                day_spots.append(p)
                used.add(id(p))

        # 某个片区还有一整天的量时，先留给它自己的那天，别提前抽调；
        # 但今天已经是最后一天，再留就永远排不上了
        pending = _pending_by_area(pool, used, areas) if d < days - 1 else {}

        # 逐轮挑「离得近 + 时段接得上 + 评分够 + 不超预算」的那一个。
        # 数量排到 per 之后不立刻收手：只要时段还能接上、游览时长仍在预算内，
        # 就再补一处，把下午和傍晚填满（上限 ceiling）
        while len(day_spots) < ceiling:
            nxt = _pick(day_spots, pool, used, pending, areas, per, interests,
                        _budget_for(day_spots, budget))
            if nxt is None:
                break
            if len(day_spots) >= per:
                if _period_gap(day_spots, nxt) > _MAX_PERIOD_GAP:
                    break
                if _minutes_of(day_spots) + _visit_of(nxt) > _budget_for(day_spots, budget):
                    break
            day_spots.append(nxt)
            used.add(id(nxt))

        if not day_spots:
            break
        ordered = _optimize_day(day_spots)
        plan.append({
            "index": d + 1,
            "date": _day_date(start_date, d),
            "theme": _theme(day_spots),
            "spots": ordered,
        })
    return plan


def _minutes_of(spots: List[dict]) -> int:
    return sum(_visit_of(s) for s in spots)


def _budget_for(day_spots: List[dict], budget: int) -> int:
    """当天预算：全夜场时按夜间窗口算，否则按节奏给的那份。"""
    if day_spots and all(str(s.get("period") or "") in ("dusk", "night") for s in day_spots):
        return _NIGHT_BUDGET
    return budget


def _period_gap(day_spots: List[dict], candidate: dict) -> int:
    """候选与当天已选景点之间最近的时段档位差。0 = 同一档，越大越不搭。"""
    if not day_spots:
        return 0
    idx = period_index(candidate.get("period"))
    return min(abs(idx - period_index(s.get("period"))) for s in day_spots)


def _pick(day_spots: List[dict], pool: List[dict], used: set,
          pending: Dict[int, int], areas: Dict[int, int], per: int,
          interests: List[str], budget: int) -> dict | None:
    """当天还缺点什么：把距离、时段弧线、评分、时长预算折成一个代价。"""
    near = _near_key(day_spots)
    used_idx = [period_index(s.get("period")) for s in day_spots]
    used_minutes = _minutes_of(day_spots)
    cats = {p.get("category") for p in day_spots}

    best, best_cost = None, None
    for p in pool:
        if id(p) in used:
            continue
        if pending.get(areas.get(id(p)), 0) >= per:
            continue                      # 该片区还够一整天，留给它自己的那天

        cost = min(near(p), 25.0)         # 距离（km），跨城会明显吃亏
        cost += max(0.0, 5.0 - _score(p, interests)) * _SCORE_KM
        if cats and p.get("category") in cats:
            cost += _CAT_COST

        if used_idx:
            idx = period_index(p.get("period"))
            gap = min(abs(idx - q) for q in used_idx)
            if gap > _MAX_PERIOD_GAP:
                cost += _GAP_COST        # 上午 + 夜晚这种，中间必空半天
            elif gap:
                cost += _NEAR_GAP_COST
            if used_idx.count(idx) >= 2:
                cost += _CAT_COST        # 同一个时段已经两处了，别再往一堆里塞
        total = used_minutes + _visit_of(p)
        if total > budget + _BUDGET_SLACK:
            cost += _FAR_OVER_BUDGET_COST
        elif total > budget:
            cost += _OVER_BUDGET_COST

        if best_cost is None or cost < best_cost:
            best, best_cost = p, cost
    return best



def _optimize_day(day_spots: List[dict]) -> List[dict]:
    """单日最优路线 + 时间窗求解；求解失败退回原来的贪心串法。"""
    ordered = optimizer.order(day_spots)
    if ordered:
        return ordered
    return _assign_times(_order_by_geo(day_spots))


def _cluster_areas(pool: List[dict], radius_km: float = 2.2) -> Dict[int, int]:
    """按坐标把候选景点聚成片区（同片区排在同一天，避免来回跑）。

    高德的行政区（adname）粒度太粗——鼓浪屿也标成「思明区」，所以这里直接用
    坐标做单遍贪心聚类：离已有片区中心 2.2km 内就并入，否则新开一个片区。
    """
    centers: List[tuple] = []
    member: Dict[int, int] = {}
    for p in pool:
        lon, lat = p.get("lon"), p.get("lat")
        if not lon or not lat:
            continue
        best, best_d = None, None
        for idx, (clon, clat) in enumerate(centers):
            d = haversine(lon, lat, clon, clat)
            if best_d is None or d < best_d:
                best, best_d = idx, d
        if best is not None and best_d is not None and best_d <= radius_km:
            member[id(p)] = best
            clon, clat = centers[best]
            centers[best] = ((clon + lon) / 2, (clat + lat) / 2)
        else:
            centers.append((lon, lat))
            member[id(p)] = len(centers) - 1
    return member


def _pending_by_area(pool: List[dict], used: set, areas: Dict[int, int]) -> Dict[int, int]:
    """每个片区还剩多少未安排的景点。"""
    cnt: Dict[int, int] = {}
    for q in pool:
        if id(q) in used:
            continue
        area = areas.get(id(q))
        if area is not None:
            cnt[area] = cnt.get(area, 0) + 1
    return cnt


def _near_key(anchors: List[dict]):
    """排序键：候选到「当天已选景点中最近的一个」的距离，越小越靠前。

    只按首站算会出事——第二站选到 10km 外之后，第三站还会按首站的距离挑，
    结果整天来回折返。这里用最近邻距离，路线自然成串。
    """
    pts = [(p.get("lon"), p.get("lat")) for p in anchors if p.get("lon") and p.get("lat")]

    def key(p: dict) -> float:
        plon, plat = p.get("lon"), p.get("lat")
        if not pts or not (plon and plat):
            return 1e9
        return min(haversine(plon, plat, alon, alat) for alon, alat in pts)

    return key


def _order_by_geo(spots: List[dict]) -> List[dict]:
    """降级排序：先按时段粗排（上午→夜晚），再在相邻时段之间用最近邻收尾。"""
    if not spots:
        return spots
    buckets: Dict[int, List[dict]] = {}
    for s in spots:
        buckets.setdefault(period_index(s.get("period")), []).append(s)

    route: List[dict] = []
    cur: dict | None = None
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda s: -float(s.get("rating") or 4))
        while group:
            if cur is None:
                nxt = group.pop(0)
            else:
                clon, clat = cur.get("lon"), cur.get("lat")
                group.sort(key=lambda s: haversine(clon, clat, s["lon"], s["lat"])
                           if (clon and clat and s.get("lon") and s.get("lat")) else 1e9)
                nxt = group.pop(0)
            route.append(nxt)
            cur = nxt
    return route


def _fmt(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _assign_times(spots: List[dict]) -> List[dict]:
    out: List[dict] = []
    t = 9 * 60  # 09:00 出发
    lunch_done = False
    for s in spots:
        if not lunch_done and t >= 11 * 60:
            ls = max(t, 12 * 60)
            prev = out[-1] if out else {}
            out.append({
                "name": "午餐", "category": "美食",
                "address": prev.get("address") or s.get("address"),
                "lon": prev.get("lon") or s.get("lon"),
                "lat": prev.get("lat") or s.get("lat"),
                "arrive": _fmt(ls), "leave": _fmt(ls + 60),
                "tip": "就近用餐，留白半小时", "source": "meal",
            })
            t = ls + 60
            lunch_done = True
        dur = int(s.get("visit", _VISIT_DEFAULT))
        arr, lev = t, t + dur
        out.append({
            "name": s.get("name"), "category": s.get("category"),
            "address": s.get("address"), "lon": s.get("lon"), "lat": s.get("lat"),
            "arrive": _fmt(arr), "leave": _fmt(lev),
            "tip": s.get("tip"), "source": s.get("source", "builtin"),
            "rating": s.get("rating"), "photo": s.get("photo"),
            "tel": s.get("tel"), "cost": s.get("cost"),
            "open_time": s.get("open_time"),
            "period": s.get("period"), "period_label": s.get("period_label"),
        })
        t = lev + _TRANSIT_BUFFER

    if not lunch_done and out:
        # 一天结束太早时补上午餐，避免出现「11:45 就结束」的空荡一天
        tail = out[-1]
        last_leave = _to_minutes(tail.get("leave")) or t
        if last_leave < 13 * 60:
            start = max(12 * 60, last_leave + _TRANSIT_BUFFER)
            out.append({
                "name": "午餐", "category": "美食",
                "address": tail.get("address"),
                "lon": tail.get("lon"), "lat": tail.get("lat"),
                "arrive": _fmt(start), "leave": _fmt(start + 60),
                "tip": "就近用餐，留白半小时", "source": "meal",
            })
    return out


def _to_minutes(value) -> int | None:
    try:
        hour, minute = str(value or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def _day_date(start_date: str | None, offset: int) -> str | None:
    if not start_date or start_date.startswith("待定"):
        return None
    try:
        y, mo, da = map(int, start_date.split("-"))
        return (date(y, mo, da) + timedelta(days=offset)).isoformat()
    except Exception:
        return None
