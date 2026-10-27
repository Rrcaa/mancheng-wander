"""行程数据准确性校验：直接吃 plan_json，逐项检查时间/重复/餐食/时段。

用法：
  ./.venv/bin/python preview/validate_plan.py <journey_id> [...]
  ./.venv/bin/python preview/validate_plan.py --all-latest 3
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "wander.db"

# 时段硬窗（与 wander/planner/timing.py 的 HARD_WINDOWS 对齐）
HARD = {
    "morning": (6 * 60, 14 * 60),
    "day": (8 * 60, 18 * 60),
    "afternoon": (11 * 60 + 30, 19 * 60 + 30),
    "dusk": (15 * 60 + 30, 22 * 60),
    "night": (17 * 60 + 30, 23 * 60 + 30),
}
LUNCH = (11 * 60, 15 * 60 + 30)
DINNER = (17 * 60 + 30, 20 * 60 + 30)


def mins(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in str(hhmm):
        return None
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def hm(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def haversine_km(a, b) -> float:
    if not a or not b:
        return float("inf")
    lat1, lng1 = float(a[0]), float(a[1])
    lat2, lng2 = float(b[0]), float(b[1])
    p = math.pi / 180
    x = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2
    return 12742 * math.asin(math.sqrt(x))


def coords(item):
    loc = item.get("location") or {}
    lat = item.get("lat") if item.get("lat") is not None else loc.get("lat")
    lng = item.get("lon") if item.get("lon") is not None else (item.get("lng") if item.get("lng") is not None else loc.get("lng"))
    if lat is None or lng is None:
        return None
    return (float(lat), float(lng))


def norm(name: str) -> str:
    s = str(name or "").strip()
    for ch in "·・()（）[]【】 ·-—、,，.。":
        s = s.replace(ch, "")
    return s.lower()


def validate(plan: dict, tag: str = "") -> list[str]:
    issues: list[str] = []
    seq = 0
    for day in plan.get("days", []):
        idx = day.get("index")
        head = f"[{tag}] D{idx}"
        items = [it for it in (day.get("spots") or []) if it.get("arrive") and it.get("leave")]
        meals = [it for it in items if it.get("type") in ("lunch", "dinner") or it.get("kind") in ("lunch", "dinner")]
        spots = [it for it in items if it not in meals]

        # 1) 重复地点
        seen: list[tuple[str, str, tuple | None]] = []
        for it in spots:
            n = norm(it.get("name"))
            c = coords(it)
            for pn, pname, pc in seen:
                if n and pn and (n == pn or (len(n) > 3 and (n in pn or pn in n))):
                    issues.append(f"{head} 重复地点：『{it.get('name')}』与『{pname}』名称重合")
                elif c and pc and haversine_km(c, pc) < 0.25:
                    issues.append(f"{head} 重复地点：『{it.get('name')}』与『{pname}』相距 {haversine_km(c, pc)*1000:.0f}m")
            seen.append((n, it.get("name"), c))

        # 2) 时间轴
        items.sort(key=lambda x: mins(x["arrive"]) or 0)
        prev = None
        for it in items:
            a, l = mins(it["arrive"]), mins(it["leave"])
            if a is None or l is None:
                issues.append(f"{head} 『{it.get('name')}』时间格式异常 {it.get('arrive')}–{it.get('leave')}")
                continue
            if l <= a:
                issues.append(f"{head} 『{it.get('name')}』离开({hm(l)}) 不晚于到达({hm(a)})")
            if prev:
                pa, pl, pname, pitem = prev
                if a < pl:
                    issues.append(f"{head} 时间冲突：『{pname}』{hm(pl)} 结束，但『{it.get('name')}』{hm(a)} 就开始")
                if a - pl >= 0:
                    dist = haversine_km(coords(prev[3]), coords(it))
                    need = dist / 0.45  # 23km/h ≈ 383m/min，取保守 450m/min 上限
                    if dist != float("inf") and a - pl < dist / 1.2:
                        issues.append(
                            f"{head} 交通时间不足：{pname} → {it.get('name')} 直线 {dist:.1f}km，"
                            f"只留 {a - pl} 分钟（需 ≥{dist/1.2:.0f}）"
                        )
            prev = (a, l, it.get("name"), it)

        # 3) 时段硬窗
        for it in spots:
            per = it.get("period")
            a, l = mins(it.get("arrive")), mins(it.get("leave"))
            if per in HARD and a is not None:
                lo, hi = HARD[per]
                if a < lo - 1 or a > hi:
                    issues.append(f"{head} 『{it.get('name')}』{hm(a)} 违反 {per} 硬窗 {hm(lo)}–{hm(hi)}")

        # 4) 餐食
        lunch = [m for m in meals if (m.get("type") or m.get("kind")) == "lunch"]
        dinner = [m for m in meals if (m.get("type") or m.get("kind")) == "dinner"]
        if not lunch:
            issues.append(f"{head} 缺午餐")
        if not dinner:
            issues.append(f"{head} 缺晚餐（当天最后一项若早于 17:00 结束，晚餐就被漏掉了）")
        # 午餐不该是当天最后一项：吃完就回酒店等于下午白空着（晚餐收尾是正常的）
        if items and items[-1] in lunch:
            issues.append(f"{head} 午餐『{items[-1].get('name')}』是当天最后一项，之后没有任何安排")
        for m in lunch:
            a = mins(m.get("arrive"))
            if a is not None and not (LUNCH[0] <= a <= LUNCH[1]):
                issues.append(f"{head} 午餐时间 {hm(a)} 不在 {hm(LUNCH[0])}–{hm(LUNCH[1])}")
        for m in dinner:
            a = mins(m.get("arrive"))
            if a is not None and not (DINNER[0] <= a <= DINNER[1]):
                issues.append(f"{head} 晚餐时间 {hm(a)} 不在 {hm(DINNER[0])}–{hm(DINNER[1])}")
        # 餐食是否退化
        for m in meals:
            nm = str(m.get("name") or "")
            if "晚餐" in nm or "午餐" in nm or not nm:
                issues.append(f"{head} 餐食未落到具体店铺：『{nm}』")
            if not m.get("rating") and not m.get("addr") and not m.get("address"):
                issues.append(f"{head} 餐食缺店铺信息：『{nm}』")

        # 5) 每日跨度
        if items:
            start = min(mins(i["arrive"]) or 9999 for i in items)
            end = max(mins(i["leave"]) or 0 for i in items)
            span = end - start
            if start < 7 * 60:
                issues.append(f"{head} 出发过早 {hm(start)}")
            if end > 23 * 60 + 30:
                issues.append(f"{head} 结束过晚 {hm(end)}")
            if span < 240:
                issues.append(f"{head} 当天只安排了 {span} 分钟（{hm(start)}–{hm(end)}），偏短")
        seq += 1
    return issues


def load(conn, jid: str):
    conn.row_factory = sqlite3.Row
    row = conn.execute("select id,title,destination,plan_json from journeys where id=?", (jid,)).fetchone()
    return dict(row) if row else None


def main() -> int:
    conn = sqlite3.connect(DB)
    args = sys.argv[1:]
    rows = []
    if args and args[0] == "--all-latest":
        n = int(args[1]) if len(args) > 1 else 3
        cur = conn.execute("select id from journeys where status!='deleted' order by rowid desc limit ?", (n,))
        rows = [load(conn, r[0]) for r in cur]
    else:
        rows = [load(conn, a) for a in args]
    total = 0
    for row in rows:
        if not row:
            print("未找到行程")
            continue
        plan = json.loads(row["plan_json"])
        issues = validate(plan, row["destination"] or row["title"])
        total += len(issues)
        print(f"\n### {row['title']} ({row['id'][:8]})  问题 {len(issues)} 条")
        for d in plan.get("days", []):
            print(f"  D{d.get('index')} theme={d.get('theme')}")
            for it in sorted(d.get("spots") or [], key=lambda x: mins(x.get("arrive")) or 0):
                kind = it.get("type") or it.get("kind") or "spot"
                print(f"    {it.get('arrive','--:--')}–{it.get('leave','--:--')}  [{kind:6}] {it.get('name')}")
        for i in issues:
            print("  ! " + i)
    print(f"\n总计问题 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
