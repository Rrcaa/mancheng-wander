"""发现页 · 热门景点。

两个问题分开解：

1.「哪些地方算热门」—— 不能用算法算。高德 /v3/place/text 的 sortrule=weight
   是通用 POI 权重（实测成都首位是「金融城双子塔」、西安首位是「城市运动公园」，
   宽窄巷子 / 锦里 / 兵马俑都不进榜），它反映本地活跃度而非游客关注度。
   所以选品用下面的 CURATED 清单，按城市维护，可月度替换 —— 这也是
   Mindtrip / 马蜂窝榜单的实际做法：榜是编辑定的，不是算出来的。

2.「数据要实时」—— 每次查询都走高德按名字重新取，评分、营业时间、照片、
   闭馆信息都是当下的值，只做内存缓存避免打爆配额。

踩过的三个坑（都写在这里，别再犯）：
  * 并发查高德 → 这个 Key 的 QPS 配额很低，一开并发就返回
    CUQPS_HAS_EXCEEDED_THE_LIMIT，而 geo.text_search 在接口失败时会退化到
    内置样例池，于是「厦门」底下出现了「故宫博物院」。所以这里串行 + 限速
    + 只认 infocode=10000 的真实结果，绝不走样例兜底。
  * 同名 POI 里混着停车场 / 售票处 / 地铁站 / 同名奶茶店，必须名字真的对得上
    才算命中，否则宁可这条不出现。
  * 没图的不能上卡片（图上压字，没图等于白块），直接筛掉。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Dict, List

import httpx

from ..config import AMAP_KEY
from . import geo

# 每城 5 个，按「第一次去大概率会去」的常识选题，城市按游客量铺开。
# 换月只需改这里，其余全部自动取实时数据。
CURATED: Dict[str, List[str]] = {
    "北京": ["故宫博物院", "八达岭长城", "颐和园", "天坛公园", "南锣鼓巷"],
    "上海": ["外滩", "武康路", "豫园", "田子坊", "上海博物馆"],
    "广州": ["广州塔", "沙面", "永庆坊", "陈家祠", "北京路步行街"],
    "重庆": ["洪崖洞", "解放碑步行街", "长江索道", "磁器口古镇", "李子坝轻轨站"],
    "成都": ["宽窄巷子", "锦里古街", "成都大熊猫繁育研究基地", "武侯祠", "春熙路"],
    "杭州": ["西湖", "灵隐寺", "河坊街", "雷峰塔", "西溪湿地"],
    "西安": ["秦始皇兵马俑博物馆", "大雁塔", "回民街", "西安城墙", "大唐不夜城"],
    "南京": ["中山陵", "夫子庙", "总统府", "南京博物院", "老门东"],
    "武汉": ["黄鹤楼", "东湖", "户部巷", "武汉大学", "江汉路步行街"],
    "长沙": ["橘子洲", "岳麓山", "太平老街", "湖南省博物馆", "超级文和友"],
    "厦门": ["鼓浪屿", "曾厝垵", "沙坡尾", "环岛路", "南普陀寺"],
    "青岛": ["栈桥", "八大关", "崂山", "青岛啤酒博物馆", "小鱼山"],
}

# 高德同名 POI 常混进这些附属设施，命中就跳过
_BAD_WORDS = ("停车", "售票", "入口", "出口", "公交", "地铁", "厕所", "服务中心", "派出所", "检票")
# 比对名字时先去掉这类通用后缀，「橘子洲」才能和「橘子洲风景名胜区」对上
_SUFFIX = re.compile(r"(风景名胜区|风景区|景区|旅游区|公园|博物院|博物馆|步行街|古镇|古街|老街|街道|轻轨站)$")
# 卡片标题要短：只剥掉「景区 / 步行街」这类不承载信息的尾巴，博物馆这种保留
_TAIL = re.compile(r"(风景名胜区|风景区|景区|旅游区|历史文化街区|历史文化名街|步行街|古镇|古街|老街|轻轨站|观景平台)$")

_GAP = 0.26          # 两次高德请求的最小间隔（实测这个 Key 串行不会超配额）
_ATTEMPTS = 4        # 被限流时最多重试几次
_CACHE: dict = {}
_TTL = 3600.0        # 完整结果缓存 1 小时
_RETRY_TTL = 120.0   # 不完整（被限流）时 2 分钟后就重试

_gate = threading.Lock()
_last_call = [0.0]
_build_lock = threading.Lock()
_progress: dict = {"running": False, "items": []}


def _core(text: str) -> str:
    text = (text or "").strip()
    return _SUFFIX.sub("", text) or text


def _normalize(name: str, city: str) -> str:
    """去掉城市前缀和通用后缀，只留可比的词干：「上海豫园」→「豫园」。"""
    return _core((name or "").replace(city, "").strip())


def _name_matches(wanted: str, name: str, city: str) -> bool:
    """名字必须真的对得上，否则宁可这条不出，也不要换成别的景点。"""
    if not name:
        return False
    core = _core(wanted)
    norm = _normalize(name, city)
    if not norm:
        return False
    return norm == core or norm.startswith(core) or core.startswith(norm)


def _amap_text(city: str, keyword: str, limit: int = 5) -> List[dict]:
    """限速 + 重试地查一次高德，只返回真正成功的 pois。"""
    if not AMAP_KEY:
        return []
    params = {
        "keywords": keyword, "city": city, "citylimit": "true",
        "offset": min(limit, 25), "page": 1, "extensions": "all", "key": AMAP_KEY,
    }
    for attempt in range(_ATTEMPTS):
        with _gate:
            wait = _GAP - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            resp = httpx.get(f"{geo._WEB_BASE}/v3/place/text", params=params, timeout=8.0)
            data = resp.json()
        except Exception:
            time.sleep(0.4 * (attempt + 1))
            continue
        if str(data.get("status")) == "1":
            return data.get("pois") or []
        # 配额问题退避重试；其它情况（比如真的搜不到）不必再试
        if "QPS" in str(data.get("info") or "") or "LIMIT" in str(data.get("info") or ""):
            time.sleep(0.5 * (attempt + 1))
            continue
        return []
    return []


def _pick_photo(photos) -> str:
    """高德一个 POI 会给好几张图，质量差得很远：

    * store.is.autonavi.com —— 官方图，固定 500px，常常是「石碑 / 大门口 / 夜景糊片」
    * aos-comment.amap.com  —— 用户实拍原图，1000–2048px，URL 里带 `_宽_高_质量`

    所以优先挑用户实拍，再按「宽高比接近 4:3（卡片比例，裁切损失最小）」
    「像素越大越清晰」排序。挑不到实拍才退回官方图。
    """
    best, best_score = "", -9e9
    for photo in photos or []:
        url = (photo or {}).get("url") or ""
        if not url:
            continue
        url = url.replace("http://", "https://", 1)
        if "aos-comment.amap.com" in url:
            score = 10.0
            size = re.search(r"_(\d{2,5})_(\d{2,5})_\d+\.", url)
            if size:
                w, h = int(size.group(1)), int(size.group(2))
                score -= abs(w / max(h, 1) - 4 / 3) * 2   # 越接近 4:3 越好裁
                score += min(w, 2400) / 4000.0            # 越大越清晰
        else:
            score = 1.0
        if score > best_score:
            best, best_score = url, score
    return best


def _lookup(city: str, wanted: str) -> dict | None:
    """按名字查一个地点，同名结果里挑「有图 + 有评分 + 名字最贴」的那条。"""
    pois = _amap_text(city, wanted, limit=5)
    best, best_score = None, -1.0
    for raw in pois:
        item = geo._poi_out(raw)
        name = item.get("name") or ""
        if not name or not item.get("location"):
            continue
        if any(word in name for word in _BAD_WORDS):
            continue
        if not _name_matches(wanted, name, city):
            continue
        photo = _pick_photo(raw.get("photos"))
        if photo:
            item["photo"] = photo
        score = 0.0
        if item.get("photo"):
            score += 3
        if item.get("rating"):
            score += item["rating"] / 2      # 4.9 分压过 3.0 分的同名子景点
        if _normalize(name, city) == _core(wanted):
            score += 2
        if score > best_score:
            best, best_score = item, score
    return best


def _month_shift() -> int:
    """按月轮换起始城市，让每月首屏换一批地方露脸（顺序变了，内容仍是同一份编辑榜）。"""
    return time.localtime().tm_mon % len(CURATED)


def _display(text: str) -> str:
    """卡片标题：「李子坝轻轨站」→「李子坝」，连着剥两层也没关系。"""
    out = (text or "").strip()
    for _ in range(3):
        shorter = _TAIL.sub("", out)
        if shorter == out or len(shorter) < 2:
            break
        out = shorter
    return out or (text or "").strip()


def _jobs() -> list:
    """交错展开：先取每城第 1 个，再取第 2 个……前 12 条就能覆盖 12 个城市。

    起始城市按月偏移，所以每个月首屏的组合都不一样。
    """
    cities = list(CURATED.keys())
    shift = _month_shift()
    cities = cities[shift:] + cities[:shift]
    out = []
    order = 0
    for depth in range(max(len(v) for v in CURATED.values())):
        for city in cities:
            names = CURATED[city]
            if depth < len(names):
                order += 1
                out.append((city, names[depth], order))
    return out


def _build() -> List[dict]:
    items: List[dict] = []
    for city, wanted, order in _jobs():
        item = _lookup(city, wanted)
        if not item or not item.get("photo"):
            continue
        item["city"] = city
        item["wanted"] = wanted
        item["title"] = _display(wanted) or item.get("name") or ""
        item["order"] = order
        items.append(item)
        # 边建边给：前端能一张张亮出来，不用等 20 秒
        _progress["items"] = list(items)
    return items


def hot_places(limit: int = 12, force: bool = False, wait: bool = False) -> List[dict]:
    """跨城市的热门景点，按清单顺序交替取样，避免整屏都是同一个城市。

    wait=False 时不阻塞：缓存没准备好就先把已抓到的部分给前端，剩下的边跑边补。
    """
    cached = _CACHE.get("hot")
    if cached and not force and time.time() - cached[0] < cached[2]:
        return cached[1][:limit]

    start = False
    with _build_lock:
        if not _progress["running"]:
            _progress.update(running=True, items=[])
            start = True
    if start:
        if wait:
            _progress["items"] = _build()
            _CACHE["hot"] = (time.time(), _progress["items"],
                             _TTL if len(_progress["items"]) >= 24 else _RETRY_TTL)
            _progress["running"] = False
        else:
            def runner():
                try:
                    _progress["items"] = _build()
                    _CACHE["hot"] = (time.time(), _progress["items"],
                                     _TTL if len(_progress["items"]) >= 24 else _RETRY_TTL)
                finally:
                    _progress["running"] = False
            threading.Thread(target=runner, daemon=True).start()
    return list(_progress["items"])[:limit]
