"""餐食编排：把「午餐 / 晚餐」占位换成真实餐厅。

原则（对齐产品预期）：
1. 餐厅要**同时靠近上下两个景点**，优先取两点连线的中点附近；
2. 只推荐**这座城市自己的特色**（本帮菜、盐水鸭、火锅…），不要泛泛的「就近用餐」；
3. 拿不到真实 POI（没配 Key 或该城市无数据）时，退化成一句具体的特色建议，
   仍然锚定在前后两站之间，不编造店名。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .geo import nearby
from .scheduler import haversine

# 高德的关键词匹配很松（搜「小笼包」可能回一条叫「外滩」的景点），
# 所以候选必须自己再判一次「到底是不是吃饭的地方」。
_FOOD_HINT = re.compile(r"餐饮|餐厅|美食|小吃|饭店|面馆|火锅|烧烤|甜品|咖啡|快餐|自助")
_NOT_FOOD = re.compile(
    r"风景|名胜|公园|博物馆|纪念馆|展览|步行街|购物|商场|写字楼|公司|住宅|"
    r"景区|寺庙|教堂|地铁|车站|停车场|医院|学校|银行|政府"
)
# 样例数据把「南京路步行街」「城隍庙」这类逛街点也标成美食，得靠名字再挡一层
_NOT_FOOD_NAME = re.compile(
    r"步行街|美食街|老街|夜市|广场$|公园$|景区$|博物馆|纪念馆|寺$|庙$|塔$|桥$|码头$|"
    r"工作室|影楼|摄影|写真|婚纱|宠物|美容|美发|健身|瑜伽|培训|教育"
)

# 城市 → 本地特色菜关键词（按优先级，前两个最典型）
CITY_SPECIALTIES: Dict[str, List[str]] = {
    "北京": ["北京烤鸭", "京菜", "炸酱面", "铜锅涮肉"],
    "上海": ["本帮菜", "生煎", "小笼包", "葱油拌面"],
    "广州": ["粤菜", "早茶", "烧腊", "肠粉"],
    "深圳": ["粤菜", "潮汕牛肉火锅", "海鲜", "烧腊"],
    "东莞": ["烧鹅", "濑粉", "粤菜", "糖水"],
    "佛山": ["顺德菜", "双皮奶", "粤菜", "肠粉"],
    "珠海": ["海鲜", "粤菜", "横琴蚝", "茶餐厅"],
    "汕头": ["潮汕牛肉火锅", "粿条", "砂锅粥", "卤鹅"],
    "杭州": ["杭帮菜", "片儿川", "小笼包", "葱包桧"],
    "南京": ["盐水鸭", "鸭血粉丝汤", "汤包", "南京菜"],
    "苏州": ["苏帮菜", "奥灶面", "松鼠桂鱼", "生煎"],
    "无锡": ["无锡菜", "小笼包", "酱排骨", "太湖三白"],
    "扬州": ["淮扬菜", "早茶", "炒饭", "狮子头"],
    "宁波": ["宁波菜", "海鲜", "汤圆", "年糕"],
    "重庆": ["重庆火锅", "重庆小面", "江湖菜", "酸辣粉"],
    "成都": ["川菜", "火锅", "串串", "担担面"],
    "西安": ["肉夹馍", "biangbiang面", "羊肉泡馍", "葫芦头"],
    "武汉": ["热干面", "豆皮", "小龙虾", "湖北菜"],
    "长沙": ["湘菜", "米粉", "臭豆腐", "口味虾"],
    "洛阳": ["水席", "牛肉汤", "胡辣汤", "浆面条"],
    "厦门": ["沙茶面", "闽南菜", "海鲜", "土笋冻"],
    "福州": ["闽菜", "鱼丸", "佛跳墙", "锅边糊"],
    "青岛": ["海鲜", "鲁菜", "啤酒屋", "烤肉串"],
    "济南": ["鲁菜", "把子肉", "甜沫", "油旋"],
    "昆明": ["云南菜", "过桥米线", "汽锅鸡", "野生菌"],
    "贵阳": ["贵州菜", "酸汤鱼", "丝娃娃", "肠旺面"],
    "南宁": ["老友粉", "广西菜", "柠檬鸭", "卷筒粉"],
    "桂林": ["桂林米粉", "啤酒鱼", "广西菜", "油茶"],
    "大理": ["白族菜", "乳扇", "酸辣鱼", "饵丝"],
    "丽江": ["纳西菜", "腊排骨", "粑粑", "米灌肠"],
    "三亚": ["海鲜", "海南菜", "椰子鸡", "清补凉"],
    "海口": ["海南粉", "椰子鸡", "海鲜", "老爸茶"],
    "沈阳": ["东北菜", "鸡架", "烧烤", "老边饺子"],
    "哈尔滨": ["东北菜", "锅包肉", "红肠", "杀猪菜"],
    "郑州": ["河南菜", "烩面", "胡辣汤", "灌汤包"],
    "太原": ["山西菜", "刀削面", "头脑", "莜面"],
    "兰州": ["兰州牛肉面", "手抓羊肉", "酿皮", "甜醅"],
    "乌鲁木齐": ["新疆菜", "大盘鸡", "烤肉", "抓饭"],
    "拉萨": ["藏餐", "甜茶", "牦牛肉", "藏面"],
    "西宁": ["青海菜", "手抓羊肉", "酿皮", "酸奶"],
    "银川": ["宁夏菜", "手抓羊肉", "羊杂碎", "蒿子面"],
}

# 城市没收录时的兜底关键词。必须是高德真能搜到的词，
# 以前用的「本地特色菜/老字号」在多数城市搜不出东西，晚餐就只剩一句占位文案
_GENERIC: List[str] = ["特色菜", "小吃", "老字号"]

# 无论如何都排在最后的兜底：直接按「餐饮服务」类型码 050000 捞周边餐馆，
# 保证任何城市、任何偏僻景点附近都能给出真实店名
_FALLBACK_KEYWORD = "餐厅"

_LUNCH_MIN = 60
_DINNER_MIN = 90
_DINNER_EARLIEST = 16 * 60 + 30      # 早于这个点结束的白天，不硬塞晚餐
_DINNER_START = 17 * 60 + 30          # 晚餐本身不要早于 17:30 开吃
_LATEST_DINNER_START = 20 * 60       # 再晚就不排晚餐了
_COMFORT_COST = 150                  # 一顿随行饭的舒适人均上限，超过就要往后排


def _cost_of(value) -> Optional[float]:
    """高德的人均是字符串（"44.0"），解析不出来就当没有。"""
    try:
        number = float(str(value).replace("¥", "").replace("元", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _specialties(city: str) -> List[str]:
    text = str(city or "")
    for name, words in CITY_SPECIALTIES.items():
        if name and name in text:
            return list(words)
    return list(_GENERIC)


def _coords(spot: Optional[dict]):
    if not spot:
        return None
    lon, lat = spot.get("lon"), spot.get("lat")
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not lon or not lat:
        return None
    return lon, lat


def _meters(a, b) -> Optional[int]:
    if not a or not b:
        return None
    if not all(a) or not all(b):
        return None
    return int(round(haversine(a[0], a[1], b[0], b[1]) * 1000))


def _anchor(prev: Optional[dict], nxt: Optional[dict]):
    """返回（中心坐标, 半径米, 是否取了两站中点）。

    两站近就取中点（饭馆夹在中间最顺路）；两站离得远时中间往往是快速路，
    没人会在半路吃饭——这时候应该就近在前一站吃，吃完再赶下一程。
    """
    a, b = _coords(prev), _coords(nxt)
    if a and b:
        span = _meters(a, b) or 0
        if span <= 3000:
            center = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            return center, min(max(span / 2 + 500, 600), 2200), True
        return a, 1400, False
    one = a or b
    if one:
        return one, 900, False
    return None, 900, False


def _is_food(item: dict) -> bool:
    """只认高德分类里明确是餐饮的 POI，避免把「南京路步行街」当餐馆。"""
    name = str(item.get("name") or "")
    if _NOT_FOOD_NAME.search(name):
        return False
    cat = f"{item.get('category') or ''} {item.get('raw_type') or ''}"
    if _NOT_FOOD.search(cat):
        return False
    return bool(_FOOD_HINT.search(cat))


def _looks_like_anchor(name: str, prev: Optional[dict], nxt: Optional[dict]) -> bool:
    """名字和上下两站撞车（高德把景点当餐馆返回）时直接排除。"""
    for spot in (prev, nxt):
        spot_name = str((spot or {}).get("name") or "")
        if not spot_name or not name:
            continue
        if name == spot_name or name in spot_name or spot_name in name:
            return True
    return False


def _scan(city: str, center, radius: float, prev, nxt,
          offset: int = 0, at: Optional[int] = None) -> Dict[str, dict]:
    lon, lat = center
    a, b = _coords(prev), _coords(nxt)
    seen: Dict[str, dict] = {}
    keywords = _specialties(city)
    if keywords and offset:
        # 换个关键词当主角，避免一天两顿都落在同一类馆子上
        offset %= len(keywords)
        keywords = keywords[offset:] + keywords[:offset]
    # 特色菜关键词经常在中等城市搜不出东西（「本地特色菜」并不是高德认的词），
    # 末尾补一条按「餐饮服务」类型码检索的通用词，保证总能落到真实店名
    keywords = list(keywords) + [_FALLBACK_KEYWORD]
    for rank, keyword in enumerate(keywords):
        try:
            items = nearby(lat, lon, kind=keyword, radius=int(radius), limit=12)
        except Exception:  # noqa: BLE001 单个关键词失败不影响其它关键词
            continue
        for item in items or []:
            if not _is_food(item):
                continue
            loc = item.get("location") or {}
            try:
                ilon, ilat = float(loc.get("lng")), float(loc.get("lat"))
            except (TypeError, ValueError):
                continue
            if not ilon or not ilat:
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            if _looks_like_anchor(name, prev, nxt):
                continue
            # 到店时已经打烊的店不能推荐，否则用户照着走会吃闭门羹
            if _open_at(item.get("open_time"), at) is False:
                continue
            d_prev = _meters(a, (ilon, ilat))
            d_next = _meters((ilon, ilat), b)
            # 距离取「到较远那一站」的距离，保证两头都不绕
            pair = max([d for d in (d_prev, d_next) if d is not None] or [0])
            if pair > radius * 1.8 + 800:
                continue
            center_d = _meters((lon, lat), (ilon, ilat)) or 0
            rating = float(item.get("rating") or 0)
            score = pair * 0.6 + center_d * 0.4 - rating * 90
            # 顺路的一家 798 元/人的餐厅不是「好选择」：越贵越往后排
            cost_value = _cost_of(item.get("cost"))
            if cost_value is not None and cost_value > _COMFORT_COST:
                score += (cost_value - _COMFORT_COST) * 1.4
            # 店名里真带着这道本地菜 / 越靠前的关键词，优先级越高
            if any(word in name for word in _DISH_WORDS):
                score -= 220
            score -= 90 / (rank + 1)
            seen[name] = {
                "name": name,
                "address": item.get("address") or "",
                "lon": ilon, "lat": ilat,
                "rating": item.get("rating"),
                "cost": item.get("cost"),
                "photo": item.get("photo"),
                "tel": item.get("tel"),
                "open_time": item.get("open_time"),
                "meal_tag": keyword,
                "tag": _display_tag(name, keyword, _specialties(city)),
                "score": score,
                "fast": bool(_FAST_FOOD.search(name)),
                "d_prev": d_prev, "d_next": d_next,
            }
    return seen


def _open_at(raw, minute: Optional[int]) -> Optional[bool]:
    """这家店在某个时刻还开不开门；解析不出营业时间就返回 None（不参与筛选）。"""
    if minute is None:
        return None
    text = str(raw or "")
    if not text or "24" in text and "小时" in text:
        return None
    found = False
    for chunk in re.split(r"[·;；,，/\s]+", text):
        hit = re.search(r"(\d{1,2}):(\d{2})\s*[-–~至]\s*(\d{1,2}):(\d{2})", chunk)
        if not hit:
            continue
        found = True
        start = int(hit.group(1)) * 60 + int(hit.group(2))
        end = int(hit.group(3)) * 60 + int(hit.group(4))
        if end <= start:
            end += 24 * 60
        # 至少还能坐下吃半小时，否则等于白跑
        if start - 20 <= minute <= end - 30:
            return True
    return False if found else None


def _pick(city: str, prev: Optional[dict], nxt: Optional[dict],
          offset: int = 0, avoid_tags=(), at: Optional[int] = None) -> Optional[dict]:
    center, radius, between = _anchor(prev, nxt)
    if not center:
        return None
    # 不是取中点时，下一站在几十公里外，别拿它当距离约束，否则周边一家都选不出来
    far_next = nxt if between else None
    # 先就近找；近处没有像样的馆子就扩大范围再找一次，实在没有再退回近处那批
    best: Optional[dict] = None
    pool = None
    for scale in (1.0, 1.9, 3.0):
        found = _scan(city, center, radius * scale, prev, far_next, offset, at)
        if not found:
            continue
        # 先保证口碑：有 3.8 分以上的就只在里面挑
        good = {k: v for k, v in found.items() if float(v.get("rating") or 0) >= 3.8}
        if good:
            pool, best = good, None
            break
        if best is None:
            best = found
    if pool is None:
        pool = best
    if not pool:
        return None
    candidates = list(pool.values())
    # 有像样的馆子时，把连锁快餐排到后面；周边本来就没得选时就不用挑了
    if any(float(x.get("rating") or 0) >= 3.8 for x in candidates):
        for item in candidates:
            if item.get("fast") and float(item.get("rating") or 0) <= 4.2:
                item["score"] += 480
    # 再保证不重复：同一种口味已经吃过，就换个品类
    candidates = [x for x in candidates if x.get("tag") not in avoid_tags] or candidates
    return sorted(candidates, key=lambda x: x["score"])[0]


def _fallback(city: str, prev, nxt) -> dict:
    """没有真实 POI 时，给一句具体的特色建议，不编店名。"""
    tags = _specialties(city)
    tag = "、".join(tags[:2])
    coords = _coords(prev) or _coords(nxt) or (None, None)
    return {
        "address": (prev or nxt or {}).get("address") or "",
        "lon": coords[0],
        "lat": coords[1],
        "meal_tag": tags[0],
        "tip": f"就近找一家{tag}，步行范围内解决",
    }


_DISH_WORDS = (
    "本帮菜", "生煎", "小笼", "葱油拌面", "蟹黄面", "面馆", "火锅", "烤鸭", "京菜",
    "炸酱面", "涮肉", "早茶", "烧腊", "肠粉", "粤菜", "盐水鸭", "鸭血粉丝", "汤包",
    "片儿川", "杭帮菜", "江湖菜", "酸辣粉", "重庆小面", "小吃", "家常菜",
    "馄饨", "水饺", "锅贴", "拌面", "炒饭", "汤圆", "灌汤包", "烧鹅", "白切鸡",
)


_GENERIC_DISH = {"面馆", "小吃", "家常菜"}

# 连锁快餐：真店、也真开得晚，但旅行中拿它当「本地风味推荐」很掉价，重罚不排除
_FAST_FOOD = re.compile(
    r"肯德基|KFC|麦当劳|汉堡王|德克士|华莱士|必胜客|赛百味|塔斯汀|"
    r"沙县|黄焖鸡|杨国福|张亮麻辣烫|正新鸡排|蜜雪冰城|瑞幸|星巴克|CoCo|茶百道|古茗",
    re.I,
)


def _display_tag(name: str, keyword: str, city_words=()) -> str:
    """标签以店名里真实出现的品类为准，先认本地特色词，再认通用品类。
    同类词按「在店名里出现的先后」取，例如「精致粤菜·夜宵火锅」应该标粤菜。"""
    text = str(name or "")
    hits = [(text.find(w), w) for w in city_words if w and w in text]
    if hits:
        return sorted(hits)[0][1]
    hits = [(text.find(w), w) for w in _DISH_WORDS
            if w in text and w not in _GENERIC_DISH]
    if hits:
        return sorted(hits)[0][1]
    # 店名里看不出品类时，别硬贴「葱油拌面」这种关键词，退回中性说法。
    # 兜底检索用的是一个通用词「餐厅」，它不是品类，不能当标签用
    if keyword and keyword != _FALLBACK_KEYWORD and keyword in text:
        return keyword
    return "本地风味"


def _meal_tip(picked: dict) -> str:
    """一行说清「什么菜、好不好、多少钱、几点开」——卡面只显示这一句。"""
    bits: List[str] = []
    tag = str(picked.get("meal_tag") or "").strip()
    if tag and tag != "本地风味":
        bits.append(f"本地特色 {tag}")
    rating = picked.get("rating")
    if rating:
        bits.append(f"★{rating}")
    cost = str(picked.get("cost") or "").strip()
    if cost:
        cost = re.sub(r"\.0+$", "", cost)
        bits.append(f"人均 ¥{cost}")
    open_time = str(picked.get("open_time") or "").strip()
    if open_time:
        # 只留第一段营业时间，避免「10:00-14:30 · 15:30-20:30」把卡片撑满
        bits.append(open_time.split("·")[0].strip())
    return " · ".join(bits) or "本地口碑餐馆"


def _fill(meal: dict, picked: dict) -> None:
    meal["source"] = "meal"
    meal["category"] = "美食"
    if picked.get("name"):
        meal["name"] = picked["name"]
    if picked.get("address"):
        meal["address"] = picked["address"]
    if picked.get("lon") and picked.get("lat"):
        meal["lon"], meal["lat"] = picked["lon"], picked["lat"]
    for key in ("rating", "cost", "photo", "tel", "open_time", "meal_tag"):
        if picked.get(key) is not None:
            meal[key] = picked[key]
    if picked.get("name"):
        meal["meal_tag"] = picked.get("tag") or _display_tag(
            picked["name"], str(picked.get("meal_tag") or ""))
    meal["tip"] = picked.get("tip") or _meal_tip(meal)


def _is_meal(spot: dict) -> bool:
    return (spot.get("source") == "meal"
            or spot.get("type") in ("lunch", "dinner")
            or str(spot.get("name") or "") in ("午餐", "晚餐"))


def _minutes(value) -> Optional[int]:
    try:
        hour, minute = str(value or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _neighbours(spots: List[dict], index: int):
    prev = next((s for s in reversed(spots[:index]) if not _is_meal(s)), None)
    nxt = next((s for s in spots[index + 1:] if not _is_meal(s)), None)
    return prev, nxt


def _add_dinner(day: dict, city: str, used_tags=None) -> None:
    spots = day.get("spots") or []
    attractions = [s for s in spots if not _is_meal(s)]
    if len(attractions) < 2:
        return
    if any(s.get("type") == "dinner" or "晚" in str(s.get("name") or "")
           for s in spots if _is_meal(s)):
        return
    last = spots[-1] if spots and not _is_meal(spots[-1]) else next(
        (s for s in reversed(spots) if not _is_meal(s)), None)
    if not last:
        return
    leave = _minutes(last.get("leave"))
    if leave is None or leave < _DINNER_EARLIEST:
        return
    start = min(max(leave + 20, _DINNER_START), _LATEST_DINNER_START)
    if start < leave:
        start = leave
    prev = last
    nxt = None
    picked = _pick(city, prev, nxt, offset=int(day.get("index") or 1) + 1,
                   avoid_tags=used_tags or (), at=start)
    meal = {
        "name": "晚餐", "category": "美食", "type": "dinner",
        "address": picked.get("address") if picked else last.get("address"),
        "lon": picked.get("lon") if picked else last.get("lon"),
        "lat": picked.get("lat") if picked else last.get("lat"),
        "arrive": _fmt(start), "leave": _fmt(min(start + _DINNER_MIN, 23 * 60 + 30)),
        "source": "meal",
    }
    if picked:
        _fill(meal, picked)
        if used_tags is not None and meal.get("meal_tag"):
            used_tags.add(meal["meal_tag"])
    else:
        meal.update(_fallback(city, prev, nxt))
    meal["type"] = "dinner"
    spots.append(meal)
    day["spots"] = spots


def enrich_meals(days: List[dict], city: str) -> None:
    """把每天已有的午餐换成真实餐厅，必要时补一顿晚餐。"""
    used_tags = set()
    for day in days or []:
        spots = day.get("spots") or []
        for index, spot in enumerate(spots):
            if not _is_meal(spot):
                continue
            slot = "dinner" if ("晚" in str(spot.get("name") or "")
                                or spot.get("type") == "dinner") else "lunch"
            prev, nxt = _neighbours(spots, index)
            picked = _pick(city, prev, nxt,
                           offset=(int(day.get("index") or 1) - 1) * 2
                           + (1 if slot == "dinner" else 0),
                           avoid_tags=used_tags,
                           at=_minutes(spot.get("arrive")))
            if picked:
                _fill(spot, picked)
                if spot.get("meal_tag"):
                    used_tags.add(spot["meal_tag"])
            else:
                spot.update(_fallback(city, prev, nxt))
            spot["type"] = slot
        _add_dinner(day, city, used_tags)
