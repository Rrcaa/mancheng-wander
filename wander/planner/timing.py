"""景点的「最佳到访时段」。

产品要求：不要固定 09:00 出发，而是先判断每个地方**最好什么时候去**，再排时间。
做法是两层：

1. 规则层（永不失败）：从名称 / 分类 / 开放时间里读出时段线索；
2. 大模型层（可选增强）：一次性把这些景点丢给模型判断，理解「洪崖洞适合夜景」
   「下浩里适合傍晚亮灯」这类规则看不出来的常识；结果落库缓存，同城同点不重复问。

时段只有五种，任何一层给不出结论就按白天处理：
    morning 上午 / day 白天 / afternoon 下午 / dusk 傍晚 / night 夜晚
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..db import connection
from .llm import get_llm

MORNING = "morning"
DAY = "day"
AFTERNOON = "afternoon"
DUSK = "dusk"
NIGHT = "night"

PERIODS = (MORNING, DAY, AFTERNOON, DUSK, NIGHT)

# 时段判定规则的版本号：规则一改就 +1，让旧缓存自动失效
_CACHE_VERSION = "v2"

# 软窗口：求解器希望到店时间落在这个区间里（分钟）
SOFT_WINDOWS: Dict[str, Tuple[int, int]] = {
    MORNING: (8 * 60, 11 * 60 + 30),
    # 「白天」类景点（公园/古镇/寺庙）下午三四点逛完全正常，窗口只给到 15:30
    # 会让一天的第四、五站必然越界，求解器于是把出发时间一路往前压去抵罚分
    DAY: (9 * 60 + 30, 17 * 60 + 30),
    AFTERNOON: (13 * 60, 17 * 60),
    DUSK: (16 * 60, 19 * 60),
    NIGHT: (18 * 60, 21 * 60 + 30),
}

# 硬窗口：到店时间**必须**落在这个区间里，否则时段标签就是错的。
# 比软窗口宽一档，只拦「夜景点排在正午」「白天景排到深夜」这类硬伤；
# 区间内的偏好仍由软窗口按分钟计成本去引导。
HARD_WINDOWS: Dict[str, Tuple[int, int]] = {
    MORNING: (0, 14 * 60),
    DAY: (0, 18 * 60),
    AFTERNOON: (11 * 60 + 30, 19 * 60 + 30),
    DUSK: (15 * 60 + 30, 22 * 60),
    NIGHT: (17 * 60 + 30, 23 * 60 + 30),
}

# 时段在一天里的先后次序，选点时要靠它判断「这条线能不能连起来」
PERIOD_ORDER = {MORNING: 0, DAY: 1, AFTERNOON: 2, DUSK: 3, NIGHT: 4}

# 午间高温区：户外景点尽量避开，室内场馆反而最适合
NOON_PEAK = (11 * 60 + 30, 14 * 60)

LABELS: Dict[str, str] = {
    MORNING: "上午",
    DAY: "白天",
    AFTERNOON: "下午",
    DUSK: "傍晚",
    NIGHT: "夜晚",
}

# 这些词出现，基本就是「靠夜色吃饭」的地方
_NIGHT_HINT = re.compile(
    r"夜景|夜游|夜市|灯光秀|灯光节|灯会|酒吧|不夜城|夜航|游船夜|摩天|星空|天幕"
)
_DUSK_HINT = re.compile(
    r"日落|落日|夕阳|黄昏|晚霞|亮灯|观景台|江景|滨江|天际线|城市景观|大桥|洪崖洞"
)
_DAY_HINT = re.compile(r"博物馆|美术馆|展览|科技馆|纪念馆|图书馆|艺术馆|水族馆|海洋馆|会展")
# 真正「只有早上才对」的地方：早茶、早市、看日出。判断标准是错过这个点就体验不到
_MORNING_HINT = re.compile(r"早茶|早市|菜市场|日出|晨间|赶海")
# 全天都能逛、没有明显最佳时刻的地方：公园、古镇、寺庙、山湖湿地……
# 以前这些都被算成「上午」，结果一天 4 个公园全挤在 8:00–11:30 的窗口里，
# 求解器只能把出发时间一路压到 8 点，还把午餐挤到下午三点半。
_ALLDAY_HINT = re.compile(
    r"公园|园林|植物园|古镇|老街|街区|寺|庙|庵|塔|山$|湖$|湿地|登山|徒步|海滨|沙滩|广场"
)


def _text(spot: dict) -> str:
    return " ".join(str(spot.get(k) or "") for k in ("name", "category", "raw_type", "address"))


def open_window(spot: dict) -> Optional[Tuple[int, int]]:
    """从 open_win / open_time 里读出开放窗口；读不出返回 None。"""
    win = spot.get("open_win")
    if isinstance(win, (list, tuple)) and len(win) >= 2:
        try:
            lh, lm = str(win[0]).split(":")[:2]
            rh, rm = str(win[1]).split(":")[:2]
            start, end = int(lh) * 60 + int(lm), int(rh) * 60 + int(rm)
            if end > start:
                return start, end
        except (TypeError, ValueError):
            pass
    raw = str(spot.get("open_time") or "")
    for chunk in raw.replace("；", " ").replace("·", " ").split():
        if "-" not in chunk:
            continue
        left, _, right = chunk.partition("-")
        try:
            lh, lm = left.strip().split(":")[:2]
            rh, rm = right.strip().split(":")[:2]
            start, end = int(lh) * 60 + int(lm), int(rh) * 60 + int(rm)
        except (TypeError, ValueError):
            continue
        if end > start:
            return start, end
    return None


def classify_rule(spot: dict) -> str:
    """纯规则判断，保证没有大模型时也能给出合理时段。"""
    text = _text(spot)
    window = open_window(spot)
    # 傍晚就关门的地方，别指望看亮灯
    if window and window[1] <= 17 * 60 + 30:
        if _DAY_HINT.search(text):
            return DAY
        if window[1] <= 15 * 60:
            return DAY
    if _NIGHT_HINT.search(text):
        return NIGHT
    if _DUSK_HINT.search(text):
        return DUSK
    if _DAY_HINT.search(text):
        return DAY
    if _MORNING_HINT.search(text):
        return MORNING
    if _ALLDAY_HINT.search(text):
        return DAY
    return DAY


def soft_window(period: str, spot: Optional[dict] = None) -> Optional[Tuple[int, int]]:
    """时段 → 期望到店区间，并按实际开放时间收窄；收窄后不成立就不加约束。"""
    base = SOFT_WINDOWS.get(period or DAY)
    if not base:
        return None
    start, end = base
    window = open_window(spot) if spot else None
    if window:
        start, end = max(start, window[0]), min(end, window[1] - 30)
        if end - start < 30:
            return None
    return start, end


def hard_window(period: str, spot: Optional[dict] = None) -> Optional[Tuple[int, int]]:
    """时段 → 到店时间的硬边界；与营业时间取交集，交集太小就以时段为准。

    营业时间经常只写到「09:00-17:00」这种粗粒度，或者压根没写，直接相交会把
    合理的夜场卡死，所以只接受足够宽的交集。
    """
    base = HARD_WINDOWS.get(period or DAY)
    if not base:
        return None
    start, end = base
    window = open_window(spot) if spot else None
    if window:
        lo, hi = max(start, window[0]), min(end, window[1])
        if hi - lo >= 90:
            return lo, hi
    return start, end


def period_index(period: Optional[str]) -> int:
    return PERIOD_ORDER.get(str(period or DAY), PERIOD_ORDER[DAY])


_INDOOR_HINT = re.compile(
    r"博物馆|美术馆|展览|科技馆|纪念馆|图书馆|艺术馆|水族馆|海洋馆|剧院|影院|商场|购物中心|书店"
)


def is_indoor(spot: dict) -> bool:
    """室内场馆不受正午暴晒影响，中午反而最合适。"""
    return bool(_INDOOR_HINT.search(_text(spot)))


def label(period: Optional[str]) -> str:
    return LABELS.get(period or "", "")


def _cached(names: List[str]) -> Dict[str, str]:
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    try:
        with connection() as c:
            rows = c.execute(
                f"SELECT name, period FROM spot_times WHERE name IN ({marks})", tuple(names)
            ).fetchall()
    except Exception:  # noqa: BLE001  旧库没有该表时静默跳过
        return {}
    return {r["name"]: r["period"] for r in rows if r["period"] in PERIODS}


def _store(pairs: List[tuple]) -> None:
    if not pairs:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with connection() as c:
            c.executemany(
                "INSERT INTO spot_times(name, period, created_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET period=excluded.period",
                [(name, period, now) for name, period in pairs],
            )
    except Exception:  # noqa: BLE001
        pass


_SYSTEM = (
    "你是旅行排期助手，负责判断景点「一天里最适合到访的时段」。"
    "只输出 JSON，不要解释。"
)


def _ask_llm(spots: List[dict], destination: str) -> Dict[str, str]:
    llm = get_llm()
    if not llm or not spots:
        return {}
    lines = []
    for spot in spots:
        bits = [str(spot.get("name") or "")]
        if spot.get("category"):
            bits.append(f"（{spot['category']}）")
        if spot.get("open_time"):
            bits.append(f" 开放时间 {spot['open_time']}")
        if spot.get("address"):
            bits.append(f" 地址 {spot['address']}")
        lines.append("".join(bits))
    prompt = (
        f"目的地：{destination or '未知'}\n"
        "下面是一批景点，请逐个判断一天里最适合到访的时段。\n"
        "可选值只能是：morning（上午 8-11 点）、day（白天，室内场馆等不限具体时段）、"
        "afternoon（下午 13-17 点）、dusk（傍晚 16-19 点，日落/亮灯/夜景前的黄金时段）、"
        "night（夜晚 18 点后，夜景/夜市/灯光秀）。\n"
        "判断依据：这个地点最有名的玩法在什么时间最好看，以及开放时间是否允许。\n"
        "如果开放时间在 17 点前就结束，不要给 dusk / night。\n"
        '输出格式：{"items":[{"name":"景点名","best_time":"morning"}]}\n\n'
        "景点列表：\n" + "\n".join(f"- {line}" for line in lines)
    )
    try:
        raw = llm.complete_json(_SYSTEM, prompt, max_tokens=600, temperature=0.1)
        payload = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:  # noqa: BLE001  模型不可用就退回规则
        return {}
    out: Dict[str, str] = {}
    for item in payload.get("items") or []:
        name = str((item or {}).get("name") or "").strip()
        period = str((item or {}).get("best_time") or "").strip().lower()
        if name and period in PERIODS:
            out[name] = period
    return out


def _key(city: str, name: str) -> str:
    """缓存键带上城市与规则版本。

    不同城市的同名地点（人民公园）时段未必一样；更要紧的是判定规则会演进——
    不带版本号的话，规则改了之后旧结果还会一直命中缓存，怎么改都不生效。
    """
    city = str(city or "").strip()
    return f"{_CACHE_VERSION}|{city}|{name}" if city else f"{_CACHE_VERSION}|{name}"


# 一个城市的候选池里，被判成「上午最佳」的最多占这么多。
# 大模型偶尔会把整座城市的景点都标成「上午最佳」，那样每个景点的窗口只剩
# 8:00–11:30，一天塞四五处必然越界，求解器只能把出发时间一路推到 8 点——
# 用户看到的「两天都是早上八点出发」就是这么来的。
_MAX_MORNING_RATIO = 0.25


def _cap_morning(spots: List[dict], known: dict, keys: dict) -> None:
    morning = [s for s in spots if known.get(keys[id(s)]) == MORNING]
    limit = max(2, round(len(spots) * _MAX_MORNING_RATIO))
    if len(morning) <= limit:
        return
    # 真「错过就体验不到」的（早茶 / 早市 / 日出 / 赶海）优先留下
    morning.sort(key=lambda s: 0 if _MORNING_HINT.search(_text(s)) else 1)
    for spot in morning[limit:]:
        known[keys[id(spot)]] = DAY


def classify_all(spots: List[dict], destination: str = "") -> None:
    """就地给每个 spot 写入 period / period_label。"""
    if not spots:
        return
    keys = {id(s): _key(destination, s.get("name")) for s in spots}
    known = _cached([k for k in keys.values() if k])
    missing = [s for s in spots if keys[id(s)] not in known]

    fresh: List[tuple] = []
    guessed = _ask_llm(missing, destination)
    for spot in missing:
        name = str(spot.get("name") or "")
        period = guessed.get(name)
        # 开放时间不允许的时段一律不接受，避免「博物馆看夜景」
        window = open_window(spot)
        if period in (DUSK, NIGHT) and window and window[1] <= 17 * 60 + 30:
            period = None
        if period not in PERIODS:
            period = classify_rule(spot)
        known[keys[id(spot)]] = period

    _cap_morning(spots, known, keys)

    for spot in spots:
        period = known.get(keys[id(spot)]) or classify_rule(spot)
        spot["period"] = period
        spot["period_label"] = LABELS.get(period, "")
        if keys[id(spot)]:
            fresh.append((keys[id(spot)], period))
    _store(fresh)
