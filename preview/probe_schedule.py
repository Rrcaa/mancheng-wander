"""离线排程探针：不查高德、不调大模型，只验证 scheduler + optimizer 的选点与时段。

用法：./.venv/bin/python preview/probe_schedule.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wander.planner import scheduler, timing  # noqa: E402

SPOTS = [
    # 华侨城片区
    {"name": "世界之窗", "category": "主题乐园", "lon": 113.9735, "lat": 22.5385,
     "rating": 4.6, "open_time": "09:00-22:30", "visit": 180,
     "address": "深圳市南山区深南大道9037号", "raw_type": "风景名胜;主题乐园"},
    {"name": "深圳欢乐谷", "category": "主题乐园", "lon": 113.9800, "lat": 22.5452,
     "rating": 4.5, "open_time": "09:30-22:00", "visit": 180,
     "address": "深圳市南山区侨城西街18号", "raw_type": "风景名胜;主题乐园"},
    {"name": "华侨城创意文化园", "category": "文创街区", "lon": 113.9922, "lat": 22.5390,
     "rating": 4.5, "open_time": "全天开放", "visit": 90,
     "address": "深圳市南山区锦绣北街2号", "raw_type": "购物;特色街区"},
    {"name": "锦绣中华民俗村", "category": "人文景观", "lon": 113.9802, "lat": 22.5357,
     "rating": 4.4, "open_time": "10:00-21:00", "visit": 150,
     "address": "深圳市南山区深南大道9003号", "raw_type": "风景名胜"},
    # 南山 / 蛇口
    {"name": "深圳湾公园", "category": "公园", "lon": 113.9480, "lat": 22.5185,
     "rating": 4.6, "open_time": "全天开放", "visit": 90,
     "address": "深圳市南山区滨海大道", "raw_type": "风景名胜;公园"},
    {"name": "海上世界", "category": "夜景", "lon": 113.9130, "lat": 22.4830,
     "rating": 4.5, "open_time": "全天开放", "visit": 90,
     "address": "深圳市南山区望海路1128号", "raw_type": "购物;特色街区"},
    # 福田
    {"name": "深圳博物馆", "category": "博物馆", "lon": 114.0579, "lat": 22.5431,
     "rating": 4.6, "open_time": "10:00-18:00", "visit": 90,
     "address": "深圳市福田区福中路市民中心A区", "raw_type": "科教文化;博物馆"},
    {"name": "莲花山公园", "category": "公园", "lon": 114.0550, "lat": 22.5560,
     "rating": 4.6, "open_time": "06:00-22:30", "visit": 75,
     "address": "深圳市福田区红荔路6030号", "raw_type": "风景名胜;公园"},
    {"name": "平安金融中心云际观光层", "category": "观景台", "lon": 114.0555, "lat": 22.5368,
     "rating": 4.6, "open_time": "10:00-22:00", "visit": 75,
     "address": "深圳市福田区益田路5033号", "raw_type": "风景名胜;观景台"},
    # 罗湖
    {"name": "东门老街", "category": "夜市", "lon": 114.1170, "lat": 22.5470,
     "rating": 4.4, "open_time": "全天开放", "visit": 90,
     "address": "深圳市罗湖区东门中路", "raw_type": "购物;特色街区"},
    {"name": "仙湖植物园", "category": "植物园", "lon": 114.1760, "lat": 22.5800,
     "rating": 4.5, "open_time": "06:00-21:30", "visit": 120,
     "address": "深圳市罗湖区仙湖路160号", "raw_type": "风景名胜;植物园"},
    # 盐田
    {"name": "大梅沙海滨公园", "category": "海滨", "lon": 114.3120, "lat": 22.5960,
     "rating": 4.5, "open_time": "全天开放", "visit": 150,
     "address": "深圳市盐田区大梅沙", "raw_type": "风景名胜;海滩"},
]


def to_min(value: str | None) -> int | None:
    try:
        h, m = str(value or "").split(":")[:2]
        return int(h) * 60 + int(m)
    except (TypeError, ValueError):
        return None


def run(days: int = 2, pace: str = "balanced", label: str = "") -> None:
    spots = [dict(s) for s in SPOTS]
    for s in spots:
        s["period"] = timing.classify_rule(s)
        s["period_label"] = timing.label(s["period"])
    plan = scheduler.build(spots, days, [], pace, "2026-10-27")

    print(f"\n===== {label or pace} / {days} 天 =====")
    problems = 0
    for day in plan:
        print(f"\n-- Day {day['index']} 主题={day['theme']}")
        for s in day["spots"]:
            arr, lev = to_min(s.get("arrive")), to_min(s.get("leave"))
            per = s.get("period") or "-"
            tag = ""
            if per == "night" and arr is not None and arr < 17 * 60 + 30:
                tag = "  << 时段冲突：夜景排在 17:30 前"
                problems += 1
            if per == "dusk" and arr is not None and arr < 15 * 60 + 30:
                tag = "  << 时段冲突：傍晚景排在 15:30 前"
                problems += 1
            if per in ("morning", "day", "afternoon") and arr is not None and arr >= 18 * 60:
                tag = "  << 时段冲突：日类景点排在 18:00 后"
                problems += 1
            print(f"   {s.get('arrive')}-{s.get('leave')}  {s.get('name')}  "
                  f"[{s.get('category')}|{per}]{tag}")
    print(f"\n时段冲突数：{problems}")
    return problems


if __name__ == "__main__":
    total = 0
    total += run(2, "balanced", "深圳 2 日（常规）")
    total += run(2, "tight", "深圳 2 日（紧凑）")
    total += run(1, "balanced", "深圳 1 日（常规）")
    print(f"\n总时段冲突：{total}")
