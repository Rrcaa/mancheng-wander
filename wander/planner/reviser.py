"""行程修订：把对话里的一句话变成对现有行程的具体修改。

不走 LLM，规则解析：
  删掉外滩 / 把外滩换成豫园 / 加一个博物馆 / 别排太满 / 起晚一点 /
  多留一天 / 少一天

每条指令翻译成一到多个操作，直接作用在 plan_json 的 days[].spots[] 上，
返回可读的改动说明。识别不出来时返回 clarify（前端澄清卡用），不瞎猜。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import geo
from .scheduler import haversine

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_REMOVE_WORDS = r"(?:删掉|删去|删除|去掉|不要|别去|取消|拿掉)"
_ADD_WORDS = r"(?:加上|加一个|加个|增加|再加|加|再去|安排)"
_REPLACE_WORDS = r"(?:换成|替换成|替换为|改为|改成|换为|换)"

_TRAVEL_MINUTES = 15


def _is_meal(spot: dict) -> bool:
    cat = str(spot.get("category") or "")
    name = str(spot.get("name") or "")
    return spot.get("source") == "meal" or "餐" in cat or name in ("午餐", "晚餐")


def _visit_minutes(spot: dict) -> int:
    name = str(spot.get("name") or "")
    if "午餐" in name:
        return 60
    if "晚餐" in name:
        return 90
    if _is_meal(spot):
        return 60
    return 90


def _minutes(value) -> Optional[int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _hhmm(total: int) -> str:
    total = max(6 * 60, min(int(total), 23 * 60 + 59))
    return f"{total // 60:02d}:{total % 60:02d}"


def _retime_day(spots: List[dict]) -> None:
    """按顺序补齐空缺的到达/离开时间，已有时间保持不动。"""
    cursor = None
    for spot in spots:
        arrive = _minutes(spot.get("arrive"))
        leave = _minutes(spot.get("leave"))
        visit = _visit_minutes(spot)
        if arrive is None:
            arrive = leave - visit if leave is not None else (
                cursor + _TRAVEL_MINUTES if cursor is not None else 9 * 60)
        if leave is None or leave <= arrive:
            leave = arrive + visit
        spot["arrive"], spot["leave"] = _hhmm(arrive), _hhmm(leave)
        cursor = leave


def _recalc_dist(spots: List[dict]) -> None:
    prev = None
    for spot in spots:
        lat, lon = spot.get("lat"), spot.get("lon")
        if lat and lon:
            spot["dist_km"] = round(haversine(prev[1], prev[0], lon, lat), 1) if prev else None
            prev = (lat, lon)
        else:
            spot["dist_km"] = None


def _same_place(a: dict, b: dict) -> bool:
    """同名、互相包含，或相距 250 米内 —— 都算同一处地点。"""
    na = re.sub(r"\s+", "", str(a.get("name") or ""))
    nb = re.sub(r"\s+", "", str(b.get("name") or ""))
    if na and nb and (na == nb or na in nb or nb in na):
        return True
    try:
        return haversine(float(a["lon"]), float(a["lat"]),
                         float(b["lon"]), float(b["lat"])) <= 0.25
    except (KeyError, TypeError, ValueError):
        return False


def find_existing(days: List[dict], poi: dict):
    """行程里已经有这处地点时返回 (第几天, 已有名字)，用来拦住重复添加。"""
    for day in days or []:
        for spot in day.get("spots") or []:
            if _is_meal(spot):
                continue
            if _same_place(spot, poi):
                return _day_number(day, 0), str(spot.get("name") or "")
    return None


def _info_score(spot: dict) -> int:
    """一条景点信息有多全：加进来的新点往往缺 tip / visit / period，留旧的更好。"""
    return sum(bool(spot.get(k)) for k in
               ("tip", "visit", "period", "period_label", "open_time", "photo", "rating"))


def _dedupe_spots(spots: List[dict]) -> None:
    """同一天里同一处地点只留一条（信息更全的那条）。"""
    kept: List[dict] = []
    for spot in spots:
        if _is_meal(spot):
            kept.append(spot)
            continue
        dup_index = next((i for i, k in enumerate(kept) if _same_place(k, spot)), None)
        if dup_index is None:
            kept.append(spot)
        elif _info_score(spot) > _info_score(kept[dup_index]):
            kept[dup_index] = spot
    if len(kept) != len(spots):
        spots[:] = kept


def _tidy(day: dict) -> None:
    spots = day.get("spots") or []
    _dedupe_spots(spots)
    _retime_day(spots)
    _recalc_dist(spots)


def _shift_day(spots: List[dict], minutes: int) -> None:
    for spot in spots:
        arrive, leave = _minutes(spot.get("arrive")), _minutes(spot.get("leave"))
        if arrive is not None:
            spot["arrive"] = _hhmm(arrive + minutes)
        if leave is not None:
            spot["leave"] = _hhmm(leave + minutes)


def _day_number(day: dict, fallback: int) -> int:
    try:
        return int(day.get("index") or fallback)
    except (TypeError, ValueError):
        return fallback


def _day_indexes(text: str, days: List[dict]) -> List[int]:
    """「Day 2 / 第2天 / 第二天」→ 目标天；没写就返回全部天。"""
    m = re.search(r"(?:day|DAY|Day)\s*(\d+)", text) or re.search(r"第\s*(\d+|[一二两三四五六七八九十])\s*天", text)
    if m:
        raw = m.group(1)
        number = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
        for i, day in enumerate(days):
            if _day_number(day, i + 1) == number:
                return [i]
        return []
    if "最后一天" in text and days:
        return [len(days) - 1]
    return list(range(len(days)))


def _find_spot(spots: List[dict], name: str) -> Optional[int]:
    name = (name or "").strip()
    if not name:
        return None
    for i, spot in enumerate(spots):
        if name == str(spot.get("name") or ""):
            return i
    for i, spot in enumerate(spots):
        spot_name = str(spot.get("name") or "")
        if spot_name and (name in spot_name or spot_name in name):
            return i
    return None


_ORDINALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _attraction_indexes(spots: List[dict]) -> List[int]:
    return [i for i, spot in enumerate(spots) if not _is_meal(spot)]


def _positional_spot(spots: List[dict], phrase: str) -> Optional[int]:
    """「最后一个景点」「第二个」「倒数第一处」这类指代 → 具体下标。"""
    text = (phrase or "").strip()
    if not text:
        return None
    pool = _attraction_indexes(spots)
    if not pool:
        return None
    m = re.search(r"倒数第\s*(\d+|[一二两三四五六七八九十])", text)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _ORDINALS.get(raw, 0)
        return pool[-n] if 0 < n <= len(pool) else None
    m = re.search(r"第\s*(\d+|[一二两三四五六七八九十])\s*(?:个|处|站)", text)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _ORDINALS.get(raw, 0)
        return pool[n - 1] if 0 < n <= len(pool) else None
    if "最后" in text:
        return pool[-1]
    if "第一" in text:
        return pool[0]
    return None


def _resolve_spot(spots: List[dict], phrase: str) -> Optional[int]:
    guess = _positional_spot(spots, phrase)
    if guess is not None:
        return guess
    return _find_spot(spots, _clean_name(phrase))


def _clean_name(text: str) -> str:
    value = re.sub(r"[，。！？、,.!?；;：:\s]+$", "", (text or "").strip())
    value = re.sub(r"^(这个|那个|这家|那家|一个|一下|了)", "", value)
    value = re.sub(r"(吧|呢|啊|哦)$", "", value)
    return value.strip()


def _to_spot(poi: dict) -> dict:
    loc = poi.get("location") or {}
    return {
        "name": poi.get("name"),
        "category": poi.get("category") or "景点",
        "address": poi.get("address") or "",
        "lon": loc.get("lng"),
        "lat": loc.get("lat"),
        "arrive": None,
        "leave": None,
        "tip": None,
        "source": "amap",
    }


def _name_related(keyword: str, name: str) -> bool:
    """地理编码兜底时，避免把「先锋书店」匹配成「中山陵」。"""
    if not keyword or not name:
        return False
    if keyword in name or name in keyword:
        return True
    return any(keyword[i:i + 2] in name for i in range(max(0, len(keyword) - 1)))


def _lookup(city: str, keyword: str) -> Optional[dict]:
    keyword = _clean_name(keyword)
    if not keyword or keyword in ("一天", "1天", "一晚"):
        return None
    for poi in geo.text_search(city, keyword, "attraction", limit=8):
        if not poi.get("location"):
            continue
        if _name_related(keyword, str(poi.get("name") or "")):
            return poi
    return None


def apply(plan: dict, instruction: str, city: str = "") -> Tuple[dict, List[str], Optional[dict]]:
    """返回 (修改后的 plan, 改动说明, 需要澄清时的交互描述)。"""
    text = (instruction or "").strip()
    days: List[dict] = plan.get("days") or []
    if not text or not days:
        return plan, [], {"question": "这次想调整哪一天、哪一处？", "missing_fields": ["instruction"]}

    city = city or plan.get("destination") or ""
    targets = _day_indexes(text, days)

    # 1) 增减一天（先判，避免「加一天」被当成加景点）
    if re.search(r"(多|加|延长|增加).{0,2}(一天|1\s*天|一晚)", text):
        last = days[-1]
        meals = [dict(s, arrive=None, leave=None) for s in (last.get("spots") or []) if _is_meal(s)]
        days.append({"index": len(days) + 1, "date": None, "theme": "自由活动", "spots": meals})
        _tidy(days[-1])
        return plan, ["多加了一天自由活动，用来缓一缓"], None
    if re.search(r"(少|减|减掉|缩短|去掉).{0,2}(一天|1\s*天)", text):
        if len(days) <= 1:
            return plan, [], {"question": "只剩一天了，不能再减。要不要换个城市重新排？", "missing_fields": ["instruction"]}
        dropped = days.pop()
        return plan, [f"去掉了第 {_day_number(dropped, len(days) + 1)} 天"], None

    # 2) 出发时间整体移动
    shift = 0
    if re.search(r"(起晚|晚点|晚一点|不要太早|不要那么早|睡到自然醒|推迟)", text):
        shift = 60
    elif re.search(r"(早起|早点|早一点|提前)", text):
        shift = -60
    if shift:
        for i in targets:
            _shift_day(days[i].get("spots") or [], shift)
            _recalc_dist(days[i].get("spots") or [])
        scope = "每天" if len(targets) >= len(days) else "、".join(
            f"第 {_day_number(days[i], i + 1)} 天" for i in targets
        )
        return plan, [f"{scope}整体推迟 1 小时" if shift > 0 else f"{scope}整体提前 1 小时"], None

    # 3) 节奏放松
    if re.search(r"(太满|太赶|太累|紧凑|别排|不要排|轻松|放松|悠闲|慢一点|少安排|少一点|减负)", text):
        removed, touched = [], False
        for i in targets:
            spots = days[i].get("spots") or []
            attractions = [j for j, s in enumerate(spots) if not _is_meal(s)]
            if len(attractions) <= 2:
                continue
            drop = attractions[-1]
            removed.append(spots[drop].get("name"))
            spots.pop(drop)
            _tidy(days[i])
            touched = True
        if touched:
            names = "、".join(f"「{n}」" for n in removed if n)
            return plan, [f"删掉了 {names}，每天松出一段时间"], None
        return plan, [], {"question": "每天已经只排了两个景点，还想怎么松？（晚点出发／去掉某处）", "missing_fields": ["instruction"]}

    # 4) 删除某一处
    remove = re.search(_REMOVE_WORDS + r"\s*([^\s，。！？,；;]{1,14})", text)
    if remove:
        phrase = remove.group(1)
        for i in targets or range(len(days)):
            index = _resolve_spot(days[i].get("spots") or [], phrase)
            if index is None:
                continue
            spot = days[i]["spots"].pop(index)
            _tidy(days[i])
            return plan, [f"第 {_day_number(days[i], i + 1)} 天去掉了「{spot.get('name')}」"], None
        return plan, [], {"question": f"行程里没有「{_clean_name(phrase)}」，想改的是哪一处？", "missing_fields": ["instruction"]}

    # 5) 替换
    replace = re.search(r"(?:把|将)?\s*([^\s，。！？,；;]{1,14}?)\s*" + _REPLACE_WORDS + r"\s*([^\s，。！？,；;]{1,14})", text)
    if replace:
        old_phrase, new_name = replace.group(1), _clean_name(replace.group(2))
        poi = _lookup(city, new_name)
        if not poi:
            return plan, [], {"question": f"没找到「{new_name}」，换个关键词或写完整名称试试。", "missing_fields": ["instruction"]}
        for i in targets or range(len(days)):
            index = _resolve_spot(days[i].get("spots") or [], old_phrase)
            if index is None:
                continue
            old = days[i]["spots"][index]
            if str(old.get("name") or "") == str(poi.get("name") or ""):
                return plan, [], {
                    "question": f"第 {_day_number(days[i], i + 1)} 天已经是「{poi.get('name')}」了，想换成哪一处？",
                    "missing_fields": ["instruction"],
                }
            spot = _to_spot(poi)
            spot["arrive"], spot["leave"] = old.get("arrive"), old.get("leave")
            days[i]["spots"][index] = spot
            _tidy(days[i])
            return plan, [f"第 {_day_number(days[i], i + 1)} 天把「{old.get('name')}」换成了「{poi.get('name')}」"], None
        day_label = "行程里" if not targets else "第 " + "、".join(
            str(_day_number(days[i], i + 1)) for i in targets
        ) + " 天"
        return plan, [], {
            "question": f"{day_label}没找到「{_clean_name(old_phrase)}」，要换掉的是哪一处？"
                        "（可以说「把玄武湖换成总统府」）",
            "missing_fields": ["instruction"],
        }

    # 6) 增加一处
    add = re.search(_ADD_WORDS + r"\s*([^\s，。！？,；;]{1,14})", text)
    if add:
        name = _clean_name(add.group(1))
        poi = _lookup(city, name)
        if not poi:
            return plan, [], {"question": f"没找到「{name}」，换个关键词或写完整名称试试。", "missing_fields": ["instruction"]}
        existing = find_existing(days, _to_spot(poi))
        if existing:
            return plan, [f"「{existing[1]}」第 {existing[0]} 天已经安排了，不用再加一处"], None
        day_i = targets[0] if targets else len(days) - 1
        days[day_i]["spots"].append(_to_spot(poi))
        _tidy(days[day_i])
        return plan, [f"第 {_day_number(days[day_i], day_i + 1)} 天加上了「{poi.get('name')}」"], None

    return plan, [], {
        "question": "这条修改我没把握。可以说「Day 2 去掉某某」或「把某某换成某某」。",
        "missing_fields": ["instruction"],
    }
