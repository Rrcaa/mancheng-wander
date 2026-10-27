"""地理供应商抽象。

内置 BuiltinProvider（样例数据，零配置可用）；配置 AMAP_API_KEY 后自动切换到
真实高德 POI 检索。两者对上层暴露完全一致的接口，便于替换其他地图源。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
import re
import threading
import time
from typing import Any, List

import httpx

from ..config import AMAP_KEY
from ..sample_data import sample_pois


class GeoProvider(ABC):
    @abstractmethod
    def search(self, city: str, interests: List[str], limit: int = 24) -> List[dict]:
        ...


class BuiltinProvider(GeoProvider):
    def search(self, city: str, interests: List[str], limit: int = 24) -> List[dict]:
        pois = sample_pois(city)
        if interests:
            pois = sorted(pois, key=lambda p: (-_interest_score(p, interests), -p["rating"]))
        else:
            pois = sorted(pois, key=lambda p: -p["rating"])
        return pois[:limit]


class AmapProvider(GeoProvider):
    """真实高德 POI 检索。按兴趣拼关键词 + 类型码各取一轮，再按评分去重排序。"""

    _TYPE_MAP = {
        "历史": ["博物馆", "文物古迹", "风景名胜"],
        "自然": ["风景名胜", "公园广场"],
        "现代": ["购物中心", "商务住宅"],
        "宗教": ["寺庙道观", "宗教场所"],
        "亲子": ["动物园", "游乐园", "科技馆"],
    }
    # 「美食」不进景点检索：它属于餐饮偏好，用景点关键词搜会一条都搜不到
    _FOOD_ONLY = {"美食", "餐饮", "小吃", "咖啡", "夜市"}

    def search(self, city: str, interests: List[str], limit: int = 24) -> List[dict]:
        if not AMAP_KEY:
            raise RuntimeError("缺少 AMAP_API_KEY")
        keywords: List[str] = []
        for interest in interests or []:
            if interest in self._FOOD_ONLY:
                continue
            keywords += self._TYPE_MAP.get(interest, [])
        # 兜底永远带一条通用景点词：单一偏好不好把候选池搜空
        keywords.append("风景名胜")
        seen: set = set()
        keywords = [k for k in keywords if not (k in seen or seen.add(k))][:6]

        merged: dict[str, dict] = {}
        for kw in keywords[:6]:
            data = _amap_get("/v3/place/text", {
                "city": city, "citylimit": "true", "keywords": kw,
                "offset": 20, "page": 1, "extensions": "all",
            })
            for item in (data or {}).get("pois", []):
                poi = _poi_out(item)
                if not poi["name"] or not poi["location"]:
                    continue
                if not _is_attraction(poi["raw_type"], poi["name"]):
                    continue
                # 关键词检索偶尔会带回外地的同名/关联 POI（在深圳搜出黄果树瀑布）
                cityname = str(poi.get("cityname") or "")
                if city and cityname and not cityname.startswith(city.rstrip("市")):
                    continue
                home = merged.get(poi["name"])
                if home is None or (poi["rating"] or 0) > (home["rating"] or 0):
                    merged[poi["name"]] = poi

        scored = _dedupe_subs(list(merged.values()))
        if not scored:
            # 高德没结果（极少见）时退回内置样例，别让规划直接失败
            raise RuntimeError(f"高德未返回「{city}」的景点数据")

        for poi in scored:
            poi["visit"] = _visit_minutes(poi.get("category") or "")
            poi["tags"] = [poi.get("category") or "景点"]
            poi["city"] = city
            poi["source"] = "amap"
        scored.sort(key=lambda p: (-(p.get("rating") or 0), p["name"]))
        return [_flatten(p) for p in scored[:limit]]


_DUP_RADIUS_KM = 2.5


def _norm_name(name: str) -> str:
    return re.sub(r"[\s\-—–·•（）()【】\[\]/\\、,，:：]+", "", str(name or ""))


def _shared_run(a: str, b: str) -> int:
    """最长公共连续子串长度，用来判断两个名字说的是不是同一处地方。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _same_site(p: dict, q: dict) -> bool:
    """两条 POI 是不是「同一个地方」：同名 / 同园区的子景点 / 名字被写进地址里。

    「黄果树瀑布」的地址是「深南大道9003号锦绣中华民俗村内(东侧)」，它就是
    锦绣中华里的微缩景观；「汇芳园」「世外桃源」都在园博园里。这类名字毫无关系
    的点位靠名字是抓不出来的，只能看地址。
    """
    na, nb = _norm_name(p.get("name")), _norm_name(q.get("name"))
    aa, ab = _norm_name(p.get("address")), _norm_name(q.get("address"))
    if na and nb and (na in nb or nb in na):
        return True
    if na and nb and _shared_run(na, nb) >= 4:
        return True
    if na and ab and na in ab:          # 我的名字写进了你的地址
        return True
    if nb and aa and nb in aa:
        return True
    return bool(aa and ab and _shared_run(aa, ab) >= 10)   # 同一个园区地址


def _dedupe_subs(pois: List[dict]) -> List[dict]:
    """去掉「同一个景区被拆出来的子点位」。

    高德会把一个地方拆成「深圳人才公园」「深圳人才公园-潮汐广场」「深圳人才公园
    人才星光大道」三条，评分还都一样高。不合并的话，按距离选点会把一整天全排在
    同一个公园里——看着像「推荐了四个景点」，其实是同一个地方走三遍。

    规则：判定为同一地点（名字/地址任一线索）且相距 2.5km 内 → 只留主条目
    （评分高的优先，同分取名字更短的）。
    """
    from .scheduler import haversine

    def loc_of(p: dict) -> dict:
        return p.get("location") or {}

    kept: List[dict] = []
    for p in sorted(pois, key=lambda q: (-(q.get("rating") or 0),
                                         len(_norm_name(q.get("name"))))):
        if not _norm_name(p.get("name")):
            continue
        dup = False
        for q in kept:
            if not _same_site(p, q):
                continue
            a, b = loc_of(p), loc_of(q)
            try:
                near = haversine(a["lng"], a["lat"], b["lng"], b["lat"]) <= _DUP_RADIUS_KM
            except (KeyError, TypeError):
                near = True
            if near:
                dup = True
                break
        if not dup:
            kept.append(p)
    return kept


def _flatten(poi: dict) -> dict:
    """统一成排程器/转换层使用的扁平结构（lon/lat 同时保留 location）。"""
    loc = poi.get("location") or {}
    flat = dict(poi)
    flat["lon"] = loc.get("lng")
    flat["lat"] = loc.get("lat")
    return flat


_VISIT_BY_CATEGORY = [
    ("博物馆", 120), ("博物院", 120), ("纪念馆", 90), ("展览", 90),
    ("园林", 90), ("公园", 75), ("古镇", 150), ("寺", 60), ("塔", 45),
    ("山", 180), ("湖", 120), ("街", 90), ("广场", 45),
]


def _visit_minutes(category: str) -> int:
    for key, minutes in _VISIT_BY_CATEGORY:
        if key in (category or ""):
            return minutes
    return 90


def _interest_score(poi: dict, interests: List[str]) -> float:
    return sum(1 for t in poi.get("tags", []) if t in interests)


def _normalize(item: dict, city: str) -> dict:
    """兼容旧接口的扁平结构（保留给外部调用方）。"""
    poi = _poi_out(item)
    return {
        "name": poi["name"],
        "category": poi["category"],
        "address": poi["address"],
        "lon": (poi["location"] or {}).get("lng"),
        "lat": (poi["location"] or {}).get("lat"),
        "visit": _visit_minutes(poi["category"]),
        "rating": poi["rating"] or 4.5,
        "tags": [poi["category"]],
        "city": city,
        "source": "amap",
        "photo": poi["photo"],
        "tel": poi["tel"],
    }


def get_provider() -> GeoProvider:
    if AMAP_KEY:
        return _AmapWithFallback()
    return BuiltinProvider()


class _AmapWithFallback(GeoProvider):
    """高德优先；报错或无结果时自动退回内置样例，保证规划不中断。"""

    def __init__(self) -> None:
        self._amap = AmapProvider()
        self._builtin = BuiltinProvider()

    def search(self, city: str, interests: List[str], limit: int = 24) -> List[dict]:
        try:
            pois = self._amap.search(city, interests, limit)
        except Exception:
            pois = []
        if pois:
            return pois
        return self._builtin.search(city, interests, limit)


# ── 高德 Web Service：POI 检索 / 周边 / 步行路线 ──────────────
# 配置 AMAP_API_KEY 时走真实接口；没配 Key 或请求失败时用内置样例兜底，
# 保证「搜索地点 / 查看周边 / 在地图上看这一段」在零配置下也能给出结果。
_WEB_BASE = "https://restapi.amap.com"

_POI_TYPE = {
    "风景名胜": "110000", "景点": "110000", "attraction": "110000",
    "餐饮服务": "050000", "餐厅": "050000", "restaurant": "050000",
}


_AMAP_LOCK = threading.Lock()
_AMAP_LAST = [0.0]
_AMAP_GAP = 0.24                      # 高德 Web 服务 QPS 很低，串行并留间隔
_AMAP_RETRY_CODES = {"10021", "10019", "10020", "10029", "10004"}


def _amap_get(path: str, params: dict, timeout: float = 8.0, _retry: bool = True):
    """高德 Web 服务调用。

    QPS 限制很紧，连续发几条就会返回 CUQPS_HAS_EXCEEDED_THE_LIMIT。以前这里是
    直接返回 None，上层看起来就像「这一带没有餐馆」，最后退回一句占位文案——
    表现是时好时坏。现在串行 + 留间隔，被限流时等一下重试一次。
    """
    if not AMAP_KEY:
        return None
    with _AMAP_LOCK:
        wait = _AMAP_GAP - (time.time() - _AMAP_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _AMAP_LAST[0] = time.time()
    try:
        r = httpx.get(f"{_WEB_BASE}{path}", params={**params, "key": AMAP_KEY}, timeout=timeout)
        data = r.json()
    except Exception:
        return None
    if str(data.get("status")) == "1":
        return data
    if _retry and str(data.get("infocode") or "") in _AMAP_RETRY_CODES:
        time.sleep(0.7)
        return _amap_get(path, params, timeout, _retry=False)
    return None


def _loc(value):
    try:
        lng, lat = str(value or "").split(",")[:2]
        return float(lng), float(lat)
    except Exception:
        return None


def _rating(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 1) if number > 0 else None


def _text(value) -> str:
    """高德在没有值时返回 []，统一成字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0].strip()
    return ""


def _first_photo(item: dict) -> str:
    for photo in item.get("photos") or []:
        url = _text((photo or {}).get("url"))
        if url:
            return url.replace("http://", "https://", 1)
    return ""


_ATTRACTION_HINT = re.compile(
    r"景点|风景|景区|博物馆|博物|纪念馆|展览|美术|公园|园林|寺|庙|教堂|古镇|古城|"
    r"遗址|遗迹|塔|山|湖|海|岛|街|广场|动物园|植物园|乐园|海洋|水族|温泉|观景|"
    r"艺术|剧院|文化|书院|故居|陵|城墙|楼阁|湿地|瀑布|沙滩|码头"
)
_EXCLUDE_HINT = re.compile(
    r"餐饮|咖啡|酒吧|饮品|甜品|面包|蛋糕|购物|超市|便利店|商场|酒店|宾馆|旅馆|"
    r"民宿|公司|企业|工厂|银行|学校|学院|医院|药|停车场|加油站|地铁|公交|车站|"
    r"机场|政府|委员会|住宅|小区|写字楼|大厦|维修|洗车|美容|美发|KTV|网吧|棋牌|"
    r"营业厅|物流|仓储|中介|事务所"
)


def _is_attraction(raw_type: str, name: str = "") -> bool:
    """把咖啡厅、写字楼、医院之类从「景点池」里剔掉。"""
    text = f"{raw_type} {name}"
    if _EXCLUDE_HINT.search(text) and not re.search(r"大学|古镇|老街", name or ""):
        return False
    return bool(_ATTRACTION_HINT.search(text))


def _category_of(raw_type: str, fallback: str = "") -> str:
    """高德 type 形如「科教文化服务;博物馆;博物馆」，取最有辨识度的一段。"""
    parts = [p.strip() for p in (raw_type or "").split(";") if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    if parts:
        return parts[0]
    return fallback or "景点"


def _open_time(value) -> str | None:
    """高德 open_time 会把两段营业时间直接拼在一起（「08:00-18:00 08:00-16:00」），
    这里用「·」隔开，前端一行放得下也看得懂。"""
    text = _text(value)
    if not text:
        return None
    return re.sub(r"(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})\s+(?=\d{1,2}:\d{2}\s*-)", r"\1 · ", text)


# opentime2 是一整段说明文字，例如故宫：
# 「旺季4月1日至10月31日 周二至周日 08:30-17:00 16:00停止检票，淡季11月1日至3月31日 …
#   周一全天关闭，节假日营业时间以官方通知为准」
# open_time 字段在不少 5A 景区是空的，只有这段文字里有真正的时间。
_TIME_RANGE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—~至]\s*(\d{1,2}:\d{2})")
_LAST_ENTRY = re.compile(
    r"(\d{1,2}:\d{2})\s*(?:前|之前)?\s*(?:停止|截止)?\s*(?:检票|售检票|售票|入馆|入场|进园|入园|进入|入内)"
)
_CLOSED_MONDAY = re.compile(r"周\s*[一1]\s*(?:全天)?\s*(?:关闭|闭馆)|每\s*周\s*[一1]\s*闭馆")


def _season_range(text: str, keyword: str, offset: int = 0):
    """从 keyword 之后找最近的「HH:MM-HH:MM」，找不到返回 None。"""
    start = text.find(keyword, offset)
    if start < 0:
        return None
    hit = _TIME_RANGE.search(text, start)
    return hit.groups() if hit else None


def _derive_open(raw: str) -> tuple:
    """把 opentime2 的说明文字抽成（可显示的一行, 可计算的窗口）。

    窗口是 [开门, 关门, 最晚入场]，最晚入场取「停止检票」那句话里的时间。
    分旺季/淡季写法时按当前月份挑一段用于计算，显示上两段都给。
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    ranges = _TIME_RANGE.findall(text)
    if not ranges:
        return None, None

    peak = _season_range(text, "旺季")
    off = _season_range(text, "淡季")
    chosen, display, anchor = None, None, 0
    if peak and off:
        peak_at, off_at = text.find("旺季"), text.find("淡季")
        display = (f"旺季 {peak[0]}-{peak[1]} · 淡季 {off[0]}-{off[1]}" if peak_at <= off_at
                   else f"淡季 {off[0]}-{off[1]} · 旺季 {peak[0]}-{peak[1]}")
        in_peak = 4 <= datetime.now().month <= 10
        chosen = peak if in_peak else off
        anchor = peak_at if in_peak else off_at
    else:
        chosen = ranges[0]
        display = f"{chosen[0]}-{chosen[1]}"

    # 「停止检票」离所选季节那一段越近越可信：取该季节描述之后的第一条
    cutoff = None
    for hit in _LAST_ENTRY.finditer(text, anchor):
        cutoff = hit.group(1)
        break

    tail = []
    if cutoff:
        tail.append(f"{cutoff} 停止入场")
    if _CLOSED_MONDAY.search(text):
        tail.append("周一闭馆")
    if tail:
        display += " · " + " · ".join(tail)
    return display, [chosen[0], chosen[1], cutoff or chosen[1]]



def _poi_out(item: dict, distance=None) -> dict:
    loc = _loc(item.get("location")) or (item.get("lng"), item.get("lat"))
    biz = item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {}
    rating = _rating(_text(biz.get("rating")) or _text(biz.get("score")))
    cost = _text(biz.get("cost"))
    raw_type = _text(item.get("type"))
    # open_time 在不少景区是空的，真正的时间写在 opentime2 的说明里
    open_time = _open_time(biz.get("open_time") or item.get("open_time"))
    open_win = None
    derived, window = _derive_open(_text(biz.get("opentime2")) or _text(item.get("opentime2")))
    if window:
        open_win = window
        if not open_time:
            open_time = derived
    return {
        "id": _text(item.get("id")) or item.get("name") or "",
        "name": _text(item.get("name")),
        "address": _text(item.get("address")),
        "district": _text(item.get("adname")) or None,
        "cityname": _text(item.get("cityname")) or None,
        "category": _category_of(raw_type, _text(item.get("category"))),
        "raw_type": raw_type,
        "location": {"lat": loc[1], "lng": loc[0]} if loc and loc[0] and loc[1] else None,
        "rating": rating,
        "tel": _text(item.get("tel")) or None,
        "cost": cost or None,
        "open_time": open_time,
        "open_win": open_win,
        "distance": distance,
        "photo": _first_photo(item) or None,
    }


def _sample_out(item: dict, distance=None) -> dict:
    return {
        "id": item.get("name"),
        "name": item.get("name") or "",
        "address": item.get("address") or "",
        "category": item.get("category") or "景点",
        "location": {"lat": item.get("lat"), "lng": item.get("lon")} if item.get("lat") else None,
        "rating": _rating(item.get("rating")),
        "tel": None,
        "cost": None,
        "distance": distance,
        "photo": None,
    }


def _all_sample_pois():
    from ..sample_data import CITIES

    return [dict(p, city=city) for city, entry in CITIES.items() for p in entry["pois"]]


def text_search(city: str, keyword: str, kind: str = "attraction", limit: int = 12) -> List[dict]:
    """关键词搜地点（编辑行程里的「替换 / 添加地点」）。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    params = {"keywords": kw, "offset": min(limit, 25), "page": 1, "extensions": "all"}
    if city:
        params.update({"city": city, "citylimit": "true"})
    data = _amap_get("/v3/place/text", params)
    if data:
        items = [_poi_out(p) for p in data.get("pois", [])]
        items = [i for i in items if i["name"] and i["location"]]
        if items:
            return items[:limit]

    pool = sample_pois(city) or _all_sample_pois()
    hits = [
        p for p in pool
        if kw in p["name"] or kw in (p.get("address") or "") or any(kw in t for t in p.get("tags") or [])
    ]
    if not hits:
        # 没配 Key 时不做「搜不到」，退化成该城市的可选地点，避免操作直接断掉
        hits = pool
    return [_sample_out(p) for p in hits[:limit]]



def nearby(lat: float, lng: float, kind: str = "风景名胜", radius: int = 1500, limit: int = 10) -> List[dict]:
    """指定坐标周边地点（「查看周边」）。"""
    if not lat or not lng:
        return []
    params = {"location": f"{lng},{lat}", "radius": max(100, int(radius)), "offset": limit, "page": 1, "extensions": "all"}
    code = _POI_TYPE.get(kind)
    if code:
        params["types"] = code
    else:
        params["keywords"] = kind
    data = _amap_get("/v3/place/around", params)
    if data:
        items = []
        for p in data.get("pois", []):
            dist = p.get("distance")
            items.append(_poi_out(p, distance=int(dist) if str(dist).isdigit() else None))
        items = [i for i in items if i["name"]]
        if items:
            return items[:limit]

    from .scheduler import haversine

    scored = []
    for p in _all_sample_pois():
        if not p.get("lat"):
            continue
        meters = haversine(lng, lat, p["lon"], p["lat"]) * 1000
        if meters <= max(radius * 1.6, 900):
            scored.append((meters, p))
    scored.sort(key=lambda row: row[0])
    return [_sample_out(p, distance=int(m)) for m, p in scored[:limit]]


_CITY_CACHE: dict = {}


_STATIC_CACHE: dict = {}
_STATIC_TTL = 6 * 3600


def _zoom_for(span_lng: float, span_lat: float, width: int, height: int) -> int:
    """按经纬跨度 + 出图尺寸反推缩放级别，让点位大致铺满画面。"""
    import math

    ppd_x = width * 0.62 / max(span_lng, 1e-4)
    ppd_y = height * 0.62 / max(span_lat, 1e-4)
    ppd = min(ppd_x, ppd_y)
    zoom = math.log2(max(ppd, 1e-6) * 360 / 256)
    return max(3, min(17, int(round(zoom))))


def _mercator(lng: float, lat: float, zoom: int):
    import math

    size = 256 * (2 ** zoom)
    x = (lng + 180) / 360 * size
    s = max(-0.9999, min(0.9999, math.sin(math.radians(lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
    return x, y


def static_map_meta(points: List[dict], width: int = 700, height: int = 1000) -> dict | None:
    """给一组带坐标的地点算出静态底图参数（真实高德底图，Web 服务 Key 即可）。

    前端拿到 center/zoom/w/h 后用墨卡托投影自己画编号点和虚线路线，
    这样既用上了真地图，又不依赖「Web端(JS API)」类型的 Key。
    """
    if not AMAP_KEY:
        return None
    pts = [(p.get("lng"), p.get("lat")) for p in points or []]
    pts = [(float(lng), float(lat)) for lng, lat in pts if lng and lat]
    if not pts:
        return None

    lngs = [x for x, _ in pts]
    lats = [y for _, y in pts]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)
    center_lng = (min_lng + max_lng) / 2
    center_lat = (min_lat + max_lat) / 2
    w = max(160, min(int(width), 1024))
    h = max(160, min(int(height), 1024))
    zoom = _zoom_for(max_lng - min_lng, max_lat - min_lat, w, h)

    key = f"{center_lng:.5f},{center_lat:.5f},{zoom},{w},{h}"
    hit = _STATIC_CACHE.get(key)
    if not (hit and _now() - hit[0] < _STATIC_TTL):
        params = {
            "location": f"{center_lng:.6f},{center_lat:.6f}",
            "zoom": zoom,
            "size": f"{w}*{h}",
            "scale": 1,
            "traffic": 0,
        }
        try:
            r = httpx.get(f"{_WEB_BASE}/v3/staticmap", params={**params, "key": AMAP_KEY}, timeout=12.0)
        except Exception:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not r.content or "json" in ctype:
            return None
        _STATIC_CACHE[key] = (_now(), r.content)
        hit = _STATIC_CACHE[key]

    return {
        "key": key,
        "center": {"lng": center_lng, "lat": center_lat},
        "zoom": zoom,
        "width": w,
        "height": h,
    }


def static_map_bytes(key: str) -> bytes | None:
    hit = _STATIC_CACHE.get(key)
    return hit[1] if hit else None


def _now() -> float:
    import time

    return time.time()


def _city_adcode(city: str) -> str | None:
    key = (city or "").strip()
    if not key:
        return None
    if key in _ADCODE_CACHE:
        return _ADCODE_CACHE[key]
    data = _amap_get("/v3/config/district", {"keywords": key, "subdistrict": 0})
    districts = (data or {}).get("districts") or []
    code = None
    for d in districts:
        if str(d.get("level")) in ("city", "province"):
            code = _text(d.get("adcode"))
            break
    if not code and districts:
        code = _text(districts[0].get("adcode"))
    if code:
        _ADCODE_CACHE[key] = code
    return code


def weather(city: str, days: int = 5) -> List[dict]:
    """高德天气预报（Web 服务 Key）：返回未来几天的实况预报，最多 4 天。"""
    adcode = _city_adcode(city)
    if not adcode:
        return []
    cache_key = f"wx:{adcode}"
    hit = _WX_CACHE.get(cache_key)
    if hit and _now() - hit[0] < 3 * 3600:
        casts = hit[1]
    else:
        data = _amap_get("/v3/weather/weatherInfo", {"city": adcode, "extensions": "all"})
        forecasts = (data or {}).get("forecasts") or []
        casts = (forecasts[0].get("casts") if forecasts else []) or []
        if not casts:
            return []
        _WX_CACHE[cache_key] = (_now(), casts)

    out = []
    for c in casts[:max(1, int(days))]:
        out.append({
            "date": _text(c.get("date")),
            "day_weather": _text(c.get("dayweather")),
            "night_weather": _text(c.get("nightweather")),
            "day_temp": _text(c.get("daytemp")),
            "night_temp": _text(c.get("nighttemp")),
            "day_wind": _text(c.get("daywind")),
            "day_power": _text(c.get("daypower")),
        })
    return out


_ADCODE_CACHE: dict = {}
_WX_CACHE: dict = {}


def is_city(name: str) -> bool:
    """判断一个词是不是城市/区县（用于从长句里认目的地）。无 Key 时只认内置城市。"""
    key = (name or "").strip()
    if not key:
        return False
    from ..sample_data import known_cities

    if key in _CITY_CACHE:
        return _CITY_CACHE[key]
    if key in known_cities():
        _CITY_CACHE[key] = True
        return True
    data = _amap_get("/v3/config/district", {"keywords": key, "subdistrict": 0, "extensions": "base"})
    if not data:
        return False
    for district in data.get("districts") or []:
        level = str(district.get("level") or "")
        if level in ("city", "province") and str(district.get("name") or "").startswith(key):
            _CITY_CACHE[key] = True
            return True
    _CITY_CACHE[key] = False
    return False


def walking_route(origin, dest, steps: int = 24) -> List[List[float]]:
    """步行路线坐标（「在地图上看这一段」）。无 Key 时给直线插值，至少画出方向。"""
    try:
        ox, oy = float(origin[0]), float(origin[1])
        dx, dy = float(dest[0]), float(dest[1])
    except (TypeError, ValueError, IndexError):
        return []
    params = {"origin": f"{ox},{oy}", "destination": f"{dx},{dy}"}
    for path in ("/v3/direction/walking", "/v4/direction/walking"):
        data = _amap_get(path, params)
        if not data:
            continue
        raw = data.get("route") or (data.get("data") or {})
        coords: List[List[float]] = []
        for route_path in raw.get("paths", []):
            for step in route_path.get("steps", []):
                for pair in str(step.get("polyline") or "").split(";"):
                    loc = _loc(pair)
                    if loc:
                        coords.append([loc[0], loc[1]])
            if coords:
                return coords
    return [
        [ox + (dx - ox) * i / (steps - 1), oy + (dy - oy) * i / (steps - 1)]
        for i in range(steps)
    ]


_DRIVE_CACHE: dict = {}
_WALK_KM = 6.0  # 这段以内按步行算，超过就按驾车算


def _polylines(*containers) -> List[List[float]]:
    for holder in containers:
        for route_path in (holder or {}).get("paths", []) or []:
            coords: List[List[float]] = []
            for step in route_path.get("steps", []) or []:
                for pair in str(step.get("polyline") or "").split(";"):
                    loc = _loc(pair)
                    if loc:
                        coords.append([loc[0], loc[1]])
            if coords:
                return coords
    return []


def _walk_leg(a, b) -> List[List[float]]:
    params = {"origin": f"{a[0]},{a[1]}", "destination": f"{b[0]},{b[1]}"}
    for path in ("/v3/direction/walking", "/v4/direction/walking"):
        data = _amap_get(path, params)
        if not data:
            continue
        coords = _polylines(data.get("route") or {}, data.get("data") or {})
        if coords:
            return coords
    return []


def _drive_leg(a, b) -> List[List[float]]:
    data = _amap_get("/v5/direction/driving", {
        "origin": f"{a[0]},{a[1]}", "destination": f"{b[0]},{b[1]}",
        "strategy": 10, "show_fields": "polyline",
    }, timeout=12.0)
    if not data:
        return []
    return _polylines(data.get("route") or {})


def day_route(points: List[dict]) -> List[List[float]]:
    """一整天的路线坐标串，沿真实道路；按每段距离选步行还是驾车。

    高德 JS 端的 Driving/Walking 插件要「安全密钥」，而 Web 服务 Key 走 REST
    就够，所以整日路线统一由后端拼好，前端只负责画。
    """
    pts = [(float(p.get("lng")), float(p.get("lat"))) for p in points or [] if p.get("lng") and p.get("lat")]
    if len(pts) < 2:
        return []
    pts = pts[:17]
    key = "|".join(f"{lng:.6f},{lat:.6f}" for lng, lat in pts)
    hit = _DRIVE_CACHE.get(key)
    if hit and _now() - hit[0] < 6 * 3600:
        return hit[1]

    from .scheduler import haversine

    coords: List[List[float]] = []
    for a, b in zip(pts, pts[1:]):
        near = haversine(a[0], a[1], b[0], b[1]) <= _WALK_KM
        leg = (_walk_leg(a, b) or _drive_leg(a, b)) if near else (_drive_leg(a, b) or _walk_leg(a, b))
        if leg:
            coords.extend(leg)
            continue
        # 都拿不到就直线插值，至少方向对
        steps = 12
        for i in range(steps):
            coords.append([
                a[0] + (b[0] - a[0]) * i / (steps - 1),
                a[1] + (b[1] - a[1]) * i / (steps - 1),
            ])
    if coords:
        _DRIVE_CACHE[key] = (_now(), coords)
    return coords
