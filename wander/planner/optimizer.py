"""单日路线求解器（OR-Tools CP-SAT）。

规模收敛到「一天」：
把当天的景点串成一条环（起点 = 酒店/首站），同时决定每站的到店时间。

模型变量：
  arc[i, j]   布尔，表示 i 之后紧接 j
  starts[i]   整数，到店时刻（分钟）
  span        整数，当天首站出发到晚一站结束的总跨度
约束：
  开门时间、时段硬窗口（夜景点不得早于 17:30 等）
目标：
  min  travel×1.0 + span×0.3 + 闭馆迟到×5.0
       + 偏离最佳时段×3.0/分钟 + 户外景点落午间高温×14
       + 正餐时间偏离×0.35/分钟

午餐作为强制节点参与同一条回路，所以「几点吃饭、吃完去哪」由求解器一起决定，
而不是排完再硬插。

没有装 ortools 时自动退化为最近邻 + 2-opt，保证服务不会起不来。
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

try:  # 允许未安装时降级
    from ortools.sat.python import cp_model  # type: ignore
except Exception:  # noqa: BLE001
    cp_model = None  # type: ignore


DEFAULT_VISIT = 90
START_MIN = 9 * 60          # 兼容旧调用：没有时段偏好时的默认出发时间
EARLIEST_START = 8 * 60     # 最早可以 08:00 出门
DEFAULT_START = 9 * 60 + 10  # 没有约束时希望大约 09:10 出发
LATEST_START = 13 * 60      # 普通行程再晚就没法安排午餐了
LATEST_NIGHT_START = 17 * 60  # 全天夜场时，傍晚动身也合理
LUNCH_MIN, LUNCH_MAX, LUNCH_DUR = 12 * 60, 15 * 60 + 30, 60
DINNER_MIN, DINNER_MAX, DINNER_DUR = 17 * 60, 20 * 60 + 30, 90
WALK_SPEED_KMH = 4.5
DRIVE_SPEED_KMH = 22.0
DRIVE_WAIT_MIN = 8          # 打车含等车时间
WALK_LIMIT_KM = 4.0
SOLVE_SECONDS = 6.0
TIMING_WEIGHT = 3.0         # 偏离「最佳时段」的每分钟代价（远大于赶路成本）
NOON_WEIGHT = 14.0          # 户外景点落在午间高温区的代价
MEAL_WEIGHT = 0.35          # 正餐时间偏离 12:30 的每分钟代价
ENTRY_WEIGHT = 45.0         # 晚于「停止入场」到店的每分钟代价（比任何赶路成本都贵）
NOON_PEAK = (11 * 60 + 30, 14 * 60)


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def travel_minutes(a: dict, b: dict) -> int:
    """两站之间的估计通行时间：走路和打车各算一遍，取更快的那个。

    原实现「4km 内一律步行」会算出 3.8km 需要 51 分钟，反而比 11km 打车还慢，
    求解器于是宁愿跨区绕路也不走同片区的近路。
    """
    lon1, lat1 = a.get("lon"), a.get("lat")
    lon2, lat2 = b.get("lon"), b.get("lat")
    if not (lon1 and lat1 and lon2 and lat2):
        return 20
    km = haversine(lon1, lat1, lon2, lat2)
    walk = km / WALK_SPEED_KMH * 60
    drive = km / DRIVE_SPEED_KMH * 60 + DRIVE_WAIT_MIN
    return max(3, int(round(min(walk, drive))))


def _hm(value) -> Optional[int]:
    try:
        hour, minute = str(value or "").split(":")[:2]
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 24 and 0 <= minute < 60:
            return hour * 60 + minute
    except (TypeError, ValueError):
        pass
    return None


def _window(spot: dict) -> Tuple[int, int]:
    """从 open_win / open_time 里读出可用窗口，读不出就给一个宽松的默认值。"""
    win = spot.get("open_win")
    if isinstance(win, (list, tuple)) and len(win) >= 2:
        start, end = _hm(win[0]), _hm(win[1])
        if start is not None and end is not None and end > start:
            return start, end
    raw = str(spot.get("open_time") or "")
    for chunk in raw.replace("；", " ").replace("·", " ").split():
        if "-" not in chunk:
            continue
        left, _, right = chunk.partition("-")
        start, end = _hm(left), _hm(right)
        if start is not None and end is not None and end > start:
            return start, end
    # 没写营业时间的夜景类地点，别把「19:00 关门」当硬约束
    if str(spot.get("period") or "") in ("dusk", "night"):
        return 8 * 60, 22 * 60
    return 8 * 60, 19 * 60


_ENTRY_CUT = re.compile(r"(\d{1,2}:\d{2})\s*停止")


def _entry_cap(spot: dict) -> Optional[int]:
    """最晚入场时刻。景区写着「16:00 停止检票」时，16:48 到门口就是白跑一趟。"""
    win = spot.get("open_win")
    if isinstance(win, (list, tuple)) and len(win) >= 3:
        cap = _hm(win[2])
        if cap:
            return cap
    hit = _ENTRY_CUT.search(str(spot.get("open_time") or ""))
    return _hm(hit.group(1)) if hit else None


def _visit(spot: dict) -> int:
    try:
        value = int(spot.get("visit") or DEFAULT_VISIT)
    except (TypeError, ValueError):
        value = DEFAULT_VISIT
    return max(30, min(240, value))


def _fmt(mins: int) -> str:
    mins = max(0, min(24 * 60 - 1, int(mins)))
    return f"{mins // 60:02d}:{mins % 60:02d}"


# ── 求解 ────────────────────────────────────────────
def order(spots: List[dict], start_min: Optional[int] = None) -> List[dict]:
    """返回带 arrive/leave 的有序站点列表。出发时间由求解器自己决定。"""
    if not spots:
        return []
    earliest = max(EARLIEST_START, min(LATEST_START, int(start_min or EARLIEST_START)))
    if len(spots) == 1:
        return _stamp(spots, _hint_start(spots, earliest), earliest)

    seq = _cp_sat_order(spots, earliest)
    if seq is None:
        seq = _greedy_order(spots, earliest)
    return seq


def _hint_start(spots: List[dict], earliest: int) -> int:
    """没有求解器时，用第一站的时段偏好反推出发时间。"""
    from .timing import soft_window

    pref = soft_window(spots[0].get("period") or "", spots[0]) if spots else None
    if pref:
        return max(earliest, min(LATEST_START, pref[0]))
    return max(earliest, DEFAULT_START)


def _cp_sat_order(spots: List[dict], earliest: int) -> Optional[List[dict]]:
    if cp_model is None:
        return None
    try:
        # 先按「时段硬窗口」求一遍；真排不下再放宽，保证不会因为约束太紧整段退回贪心
        seq = _solve(spots, earliest, strict=True)
        if seq is not None:
            return seq
        return _solve(spots, earliest, strict=False)
    except Exception:  # noqa: BLE001  求解器异常不拖垮主流程
        return None


def _allowed(spot: dict, strict: bool) -> Tuple[int, int]:
    """到店时间的硬边界：时段窗口 + 营业时间；放宽模式不设限。"""
    if not strict:
        return 0, 24 * 60
    from .timing import hard_window

    return hard_window(spot.get("period") or "", spot) or (0, 24 * 60)


def _solve(spots: List[dict], earliest: int, strict: bool = True) -> Optional[List[dict]]:
    from .timing import is_indoor, soft_window

    n = len(spots)
    horizon = 24 * 60
    model = cp_model.CpModel()

    # 全天都是夜场（夜游 + 夜景 + 夜市）时，不该硬塞一顿午餐
    all_night = all(str(s.get("period") or "") in ("dusk", "night") for s in spots)
    meal_name = "晚餐" if all_night else "午餐"
    meal_type = "dinner" if all_night else "lunch"
    meal_dur = DINNER_DUR if all_night else LUNCH_DUR
    meal_window = (DINNER_MIN, DINNER_MAX) if all_night else (LUNCH_MIN - 30, LUNCH_MAX)
    latest_start = LATEST_NIGHT_START if all_night else LATEST_START

    # 节点 0 = 出发点，1..n = 景点，n+1 = 当天的正餐
    lunch = n + 1
    count = n + 2
    dur = [0] + [_visit(s) for s in spots] + [meal_dur]
    windows: List[Tuple[int, int]] = [(0, horizon)]
    for s in spots:
        windows.append(_window(s))
    windows.append(meal_window)

    starts = [model.NewIntVar(0, horizon, f"s{i}") for i in range(count)]
    # 出发时间不再写死：让求解器按时段偏好自己挑（最早的早上、夜场可以傍晚才动身）
    model.Add(starts[0] >= max(EARLIEST_START, min(earliest, latest_start)))
    model.Add(starts[0] <= latest_start)
    for i in range(1, count):
        # 开门时间是硬约束：早到只能等，不能提前进
        model.Add(starts[i] >= windows[i][0])

    # 时段是硬约束：夜景点不会被排到正午，白天景也不会被排到深夜
    if strict:
        for i, spot in enumerate(spots, start=1):
            lo, hi = _allowed(spot, strict)
            model.Add(starts[i] >= lo)
            model.Add(starts[i] <= hi)

    end_of = []  # 每站的最晚结束时刻（用于 span 与硬窗口）
    for i in range(count):
        late = model.NewIntVar(0, horizon, f"late{i}")
        model.Add(0 <= late)
        model.Add(late >= starts[i] + dur[i] - min(horizon, windows[i][1]))
        end_of.append(late)

    arcs: Dict[Tuple[int, int], object] = {}
    arc_literals = []
    for i in range(count):
        for j in range(count):
            if i == j:
                continue
            literal = model.NewBoolVar(f"a{i}_{j}")
            arcs[(i, j)] = literal
            arc_literals.append(literal)

    # 每个节点进出各一条（午餐也必须在回路上）
    for i in range(count):
        model.Add(sum(arcs[(i, j)] for j in range(count) if j != i) == 1)
        model.Add(sum(arcs[(j, i)] for j in range(count) if j != i) == 1)
    model.AddCircuit([(i, j, arcs[(i, j)]) for (i, j) in arcs])

    # 出门就吃饭不合理：午餐不能是第一站，必须至少先逛一处
    model.Add(arcs[(0, lunch)] == 0)

    def meal_exit(target: int) -> int:
        """吃完饭去下一站的代价。

        餐厅就在上一站附近，所以真正的路程是「上一站 → 下一站」。求解时不知道
        上一站是谁，用「离下一站最近的其他景点」近似——否则求解器会把午餐当成
        免费传送门，从南山吃完饭瞬移到福田，排出来的路线天天折返。
        """
        dists = [travel_minutes(spots[k - 1], spots[target - 1])
                 for k in range(1, n + 1) if k != target]
        return max(8, min(dists)) if dists else 8

    travel = {}
    for (i, j) in arcs:
        if j == 0:                       # 回到出发点，不计代价
            travel[(i, j)] = 0
        elif i == lunch:
            travel[(i, j)] = meal_exit(j)
        elif i == 0:                     # 从住处出发
            travel[(i, j)] = 10
        elif j == lunch:                 # 走去吃饭，就近
            travel[(i, j)] = 8
        else:
            travel[(i, j)] = travel_minutes(spots[i - 1], spots[j - 1])

    big_m = horizon
    for (i, j), literal in arcs.items():
        if j == 0:
            continue
        model.Add(starts[j] >= starts[i] + dur[i] + travel[(i, j)] - big_m * (1 - literal))

    span = model.NewIntVar(0, horizon, "span")
    model.Add(span >= starts[0])
    for i in range(1, count):
        model.Add(span >= starts[i] + dur[i] - starts[0])

    # 时段偏好：每个景点希望落在自己的「最佳到访区间」里，越界按分钟罚
    timing_dev = []
    for i, spot in enumerate(spots, start=1):
        pref = soft_window(spot.get("period") or "", spot)
        if not pref:
            continue
        too_early = model.NewIntVar(0, horizon, f"early{i}")
        too_late = model.NewIntVar(0, horizon, f"tlater{i}")
        model.Add(too_early >= pref[0] - starts[i])
        model.Add(too_late >= starts[i] - pref[1])
        timing_dev.extend([too_early, too_late])
    total_timing = sum(timing_dev) if timing_dev else 0

    # 午间高温：户外景点别在 11:30–14:00 到店，室内场馆（博物馆/水族馆）不受影响
    noon_flags = []
    if strict:
        noon_lo, noon_hi = NOON_PEAK
        for i, spot in enumerate(spots, start=1):
            if is_indoor(spot) or str(spot.get("period") or "") in ("dusk", "night"):
                continue
            before = model.NewBoolVar(f"nb{i}")
            inside = model.NewBoolVar(f"nn{i}")
            after = model.NewBoolVar(f"na{i}")
            model.AddBoolOr([before, inside, after])
            model.Add(starts[i] <= noon_lo - 1).OnlyEnforceIf(before)
            model.Add(starts[i] >= noon_lo).OnlyEnforceIf(inside)
            model.Add(starts[i] <= noon_hi).OnlyEnforceIf(inside)
            model.Add(starts[i] >= noon_hi + 1).OnlyEnforceIf(after)
            noon_flags.append(inside)
    total_noon = sum(noon_flags) if noon_flags else 0

    # 正餐别拖太晚：给一个「可接受区间」，区间内不罚，别为了对齐某个整点绕远路
    meal_lo, meal_hi = (18 * 60 + 30, 20 * 60) if all_night else (11 * 60 + 45, 13 * 60)
    meal_before = model.NewIntVar(0, horizon, "mealBefore")
    meal_after = model.NewIntVar(0, horizon, "mealAfter")
    model.Add(meal_before >= meal_lo - starts[lunch])
    model.Add(meal_after >= starts[lunch] - meal_hi)
    total_meal = meal_before + meal_after

    # 出发时间只做轻微引导：太早出门、或无缘无故拖到中午都要略贵一点
    start_early = model.NewIntVar(0, horizon, "startEarly")
    start_late = model.NewIntVar(0, horizon, "startLate")
    model.Add(start_early >= DEFAULT_START - starts[0])
    model.Add(start_late >= starts[0] - DEFAULT_START)

    total_travel = sum(travel[k] * arcs[k] for k in arcs)
    total_late = sum(end_of[1:])
    # 最晚入场：写着 16:00 停止检票的地方，16:48 到就是白跑。权重给高，但保留软约束，
    # 免得整天的点位凑不出可行解时直接退回「什么都可以」的宽松模式。
    entry_dev = []
    for i, spot in enumerate(spots, start=1):
        cap = _entry_cap(spot)
        if cap is None:
            continue
        over = model.NewIntVar(0, horizon, f"cap{i}")
        model.Add(over >= starts[i] - cap)
        entry_dev.append(over)
    total_entry = sum(entry_dev) if entry_dev else 0
    # span 权重稍大一点：一天内的空档（等傍晚、等开饭）不要拖得太长
    model.Minimize(total_travel + 0.3 * span + 5 * total_late
                   + ENTRY_WEIGHT * total_entry
                   + TIMING_WEIGHT * total_timing
                   + NOON_WEIGHT * total_noon
                   + MEAL_WEIGHT * total_meal
                   + 0.15 * start_early + 0.05 * start_late)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVE_SECONDS
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    # 还原顺序
    succ = {}
    for (i, j), literal in arcs.items():
        if solver.Value(literal):
            succ[i] = j
    seq: List[int] = []
    node = succ.get(0)
    guard = 0
    while node is not None and node != 0 and guard < count:
        seq.append(node)
        node = succ.get(node)
        guard += 1
    if len(seq) != count - 1:
        return None

    out: List[dict] = []
    for node in seq:
        begin = int(solver.Value(starts[node]))
        if node == lunch:
            out.append(_meal(spots, out, begin, meal_name, meal_type, meal_dur))
        else:
            spot = dict(spots[node - 1])
            spot["arrive"] = _fmt(begin)
            spot["leave"] = _fmt(begin + dur[node])
            out.append(spot)
    return out


def _meal(spots: List[dict], done: List[dict], begin: int,
          name: str = "午餐", kind: str = "lunch", length: int = LUNCH_DUR) -> dict:
    anchor = done[-1] if done else (spots[0] if spots else {})
    return {
        "name": name, "category": "美食", "type": kind,
        "address": anchor.get("address"), "lon": anchor.get("lon"),
        "lat": anchor.get("lat"), "arrive": _fmt(begin),
        "leave": _fmt(begin + length),
        "tip": "就近用餐，顺便歇脚", "source": "meal",
    }


def _stamp(spots: List[dict], start_min: int, _unused: int) -> List[dict]:
    out = []
    cursor = start_min
    for spot in spots:
        item = dict(spot)
        item["arrive"] = _fmt(cursor)
        item["leave"] = _fmt(cursor + _visit(spot))
        cursor += _visit(spot) + 15
        out.append(item)
    return out


# ── 降级：最近邻 + 2-opt ────────────────────────────
def _greedy_order(spots: List[dict], start_min: int) -> List[dict]:
    remaining = sorted(spots, key=lambda s: -float(s.get("rating") or 4))
    best: Optional[List[dict]] = None
    for seed in remaining[:min(3, len(remaining))]:
        route = [seed]
        pool = [s for s in remaining if s is not seed]
        while pool:
            pool.sort(key=lambda s: travel_minutes(route[-1], s))
            route.append(pool.pop(0))
        route = _two_opt(route)
        cost = sum(travel_minutes(a, b) for a, b in zip(route, route[1:]))
        if best is None or cost < best[0]:
            best = (cost, route)
    if not best:
        return []
    start = _hint_start(best[1], start_min)
    return _stamp(best[1], start, start)


def _two_opt(route: List[dict]) -> List[dict]:
    improved = True
    best = list(route)
    rounds = 0
    while improved and rounds < 30:
        improved = False
        rounds += 1
        for i in range(len(best) - 1):
            for j in range(i + 2, len(best)):
                a, b = best[i], best[i + 1]
                c, d = best[j], best[(j + 1) % len(best)]
                before = travel_minutes(a, b) + travel_minutes(c, d)
                after = travel_minutes(a, c) + travel_minutes(b, d)
                if after + 1 < before:
                    best[i + 1:j + 1] = reversed(best[i + 1:j + 1])
                    improved = True
    return best
