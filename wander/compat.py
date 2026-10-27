"""compat.py — 对齐原版前端的 API 兼容层。

原版前端（CDN React + Babel 全局脚本）通过 /api/auth、/api/history、
/api/conversations、/api/runs、/api/plan/*、/api/poi/*、/api/profile、
/api/memories 等契约与后端交互。本模块把这些契约映射到自研的规划引擎上，
保证「分毫不差」的前端能完整跑通核心流程（登录、首页、规划对话、行程详情、
编辑 / 路线优化 / 回退）。

对话 / 任务运行时采用进程内存态（重启即清空对话），行程本身仍持久化到
journeys 表，保证首页历史与详情页可回看。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse

from .db import connection
from .security import current_user
from .planner.engine import plan as run_plan
from .planner.scheduler import haversine
from .planner import geo, revision, discover
from .chat.brief_rules import required_brief_fields

logger = logging.getLogger(__name__)

router = APIRouter(tags=["compat"])

# ── 运行时（对话 / 任务 / 事件）───────────────────────
# 对话、消息、简报落 SQLite；run 与事件流保持进程内存态（重启即结束的任务）。
_lock = threading.RLock()
_conversations: Dict[str, dict] = {}
_messages: Dict[str, list] = {}
_runs: Dict[str, dict] = {}
_events: Dict[str, list] = {}
_seq_counter: Dict[str, int] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _conv_from_row(row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "status": row["status"] or "active",
        "related_itinerary_id": row["related_itinerary_id"],
        "finalization_status": row["finalization_status"],
        "memory_error_code": row["memory_error_code"],
        "memory_created": row["memory_created"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_waiting_user": False,
        "has_ready_brief": False,
        "has_active_planning": False,
        "has_unread_completed": False,
    }


def _get_conv(cid: str) -> Optional[dict]:
    if not cid:
        return None
    with _lock:
        conv = _conversations.get(cid)
    if conv is not None:
        return conv
    with connection() as c:
        row = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    conv = _conv_from_row(row)
    with _lock:
        _conversations[cid] = conv
    return conv


def _put_conv(conv: dict) -> None:
    with _lock:
        _conversations[conv["id"]] = conv
    with connection() as c:
        c.execute(
            """INSERT INTO conversations(id, owner_id, title, status, related_itinerary_id,
               finalization_status, memory_error_code, memory_created, created_at, updated_at)
               VALUES(:id,:owner_id,:title,:status,:related_itinerary_id,
                      :finalization_status,:memory_error_code,:memory_created,:created_at,:updated_at)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status,
                 related_itinerary_id=excluded.related_itinerary_id,
                 finalization_status=excluded.finalization_status,
                 memory_error_code=excluded.memory_error_code,
                 memory_created=excluded.memory_created,
                 updated_at=excluded.updated_at""",
            {
                "id": conv["id"],
                "owner_id": conv["owner_id"],
                "title": conv.get("title"),
                "status": conv.get("status") or "active",
                "related_itinerary_id": conv.get("related_itinerary_id"),
                "finalization_status": conv.get("finalization_status"),
                "memory_error_code": conv.get("memory_error_code"),
                "memory_created": conv.get("memory_created"),
                "created_at": conv.get("created_at") or _now(),
                "updated_at": conv.get("updated_at") or _now(),
            },
        )


def _get_messages(cid: str) -> list:
    with _lock:
        cached = _messages.get(cid)
    if cached is not None:
        return cached
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY sequence", (cid,)
        ).fetchall()
    msgs = [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "sequence": r["sequence"],
            "created_at": r["created_at"],
            "conversation_id": r["conversation_id"],
            "artifacts": json.loads(r["artifacts"] or "[]"),
            "related_itinerary_id": r["related_itinerary_id"],
            "related_run_id": r["related_run_id"],
        }
        for r in rows
    ]
    with _lock:
        _messages[cid] = msgs
        if msgs:
            top = max(float(m["sequence"]) for m in msgs)
            _seq_counter[cid] = max(_seq_counter.get(cid, 0), int(top))
    return msgs


def _put_message(msg: dict) -> None:
    with _lock:
        _messages.setdefault(msg["conversation_id"], []).append(msg)
    with connection() as c:
        c.execute(
            """INSERT INTO conversation_messages(id, conversation_id, role, content, sequence,
               artifacts, related_itinerary_id, related_run_id, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                msg["id"], msg["conversation_id"], msg["role"], msg.get("content") or "",
                msg["sequence"], json.dumps(msg.get("artifacts") or [], ensure_ascii=False),
                msg.get("related_itinerary_id"), msg.get("related_run_id"),
                msg.get("created_at") or _now(),
            ),
        )


def _next_seq(cid: str) -> int:
    _get_messages(cid)  # 保证计数器跨重启从库里最大值续上
    with _lock:
        n = _seq_counter.get(cid, 0) + 1
        _seq_counter[cid] = n
        return n


# ── 行程存储与适配 ──────────────────────────────────
def _fetch_journey(jid: str, uid: str):
    with connection() as c:
        row = c.execute(
            "SELECT * FROM journeys WHERE id=? AND owner_id=? AND COALESCE(status,'') != 'deleted'",
            (jid, uid),
        ).fetchone()
    if not row:
        raise HTTPException(404, "行程不存在")
    return row


def _save_plan_json(jid: str, data: dict) -> None:
    with connection() as c:
        c.execute(
            "UPDATE journeys SET plan_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), jid),
        )


def _close_enough(lat1, lng1, lat2, lng2) -> bool:
    try:
        if not all(v is not None for v in (lat1, lng1, lat2, lng2)):
            return True          # 没有坐标就只按名字判断
        from .planner.scheduler import haversine
        return haversine(float(lng1), float(lat1), float(lng2), float(lat2)) <= 0.25
    except (TypeError, ValueError):
        return True


def _to_final_plan(data: dict, plan_id: str) -> dict:
    """把自研 plan 结构（days[].spots[]）转成原版前端 adaptPlan 期望的
    final_plan 结构（days[].timeline[]）。"""
    days_out: List[dict] = []
    start_date = data.get("start_date")
    for d in data.get("days", []):
        timeline: List[dict] = []
        prev_loc = None
        seen_spots: List[tuple] = []   # 同一天的 (名字, 纬度, 经度)，用来挡住重复项
        for s in d.get("spots", []):
            lat, lng = s.get("lat"), s.get("lon")
            loc = {"lat": lat, "lng": lng}
            cat = str(s.get("category") or "")
            is_meal = (s.get("source") == "meal" or s.get("type") in ("lunch", "dinner")
                       or "餐" in cat or s.get("name") in ("午餐", "晚餐"))
            # 历史行程里可能有编辑时重复 append 的同一处地点，展示时只留第一条
            if not is_meal:
                key = (str(s.get("name") or "").strip(), lat, lng)
                if any(key[0] and key[0] == n and _close_enough(lat, lng, la, lo)
                       for n, la, lo in seen_spots):
                    continue
                seen_spots.append(key)
            # 相邻两站的真实直线距离：前端「导航」行与餐食卡都用它
            dist_km = None
            if prev_loc and lat and lng and prev_loc.get("lat") and prev_loc.get("lng"):
                try:
                    from .planner.scheduler import haversine
                    dist_km = round(haversine(prev_loc["lng"], prev_loc["lat"], lng, lat), 2)
                except Exception:  # noqa: BLE001
                    dist_km = None
            if lat and lng:
                prev_loc = {"lat": lat, "lng": lng}
            if is_meal:
                timeline.append({
                    "type": "dinner" if (s.get("type") == "dinner"
                                         or "晚" in str(s.get("name") or "")) else "lunch",
                    "name": s.get("name"),
                    "start_time": s.get("arrive"),
                    "end_time": s.get("leave"),
                    "address": s.get("address"),
                    "location": loc,
                    "rating": s.get("rating"),
                    "cost": s.get("cost"),
                    "reason": s.get("tip"),
                    "photo": s.get("photo"),
                    "tel": s.get("tel"),
                    "open_time": s.get("open_time"),
                    "category": "美食",
                    "meal_tag": s.get("meal_tag"),
                    "dist_from_prev_km": dist_km,
                })
            else:
                timeline.append({
                    "type": "attraction",
                    "name": s.get("name"),
                    "start_time": s.get("arrive"),
                    "end_time": s.get("leave"),
                    "period": s.get("period"),
                    "period_label": s.get("period_label"),
                    "rating": s.get("rating"),
                    "open_time": s.get("open_time"),
                    "photo": s.get("photo"),
                    "tip": s.get("tip"),
                    "location": loc,
                    "address": s.get("address"),
                    "tel": s.get("tel"),
                    "cost": s.get("cost"),
                    "dist_from_prev_km": dist_km,
                })
        days_out.append({
            "day": d.get("index"),
            "date": d.get("date"),
            "theme": d.get("theme"),
            "timeline": timeline,
        })

    if not start_date and days_out:
        start_date = days_out[0].get("date")

    end_date = None
    if start_date and days_out:
        try:
            y, m, dd = map(int, start_date.split("-")[:3])
            end_date = (_date(y, m, dd) + timedelta(days=len(days_out) - 1)).isoformat()
        except Exception:
            end_date = None

    return {
        "plan_id": plan_id,
        "destination": data.get("destination") or "旅行",
        "days_count": len(days_out),
        "start_date": start_date,
        "end_date": end_date,
        "preferences": {},
        "day_themes": {str(d["day"]): d["theme"] for d in days_out if d.get("theme")},
        "candidate_spots": [],
        "weather_forecast": _weather_for(data.get("destination") or "", days_out),
        "hotel": data.get("hotel") or "",
        "notes": data.get("notes") or "",
        "tip_status": "succeeded",
        "route_issues": [],
        "history": [],
        "days": days_out,
    }


def _weather_for(destination: str, days_out: List[dict]) -> List[Optional[dict]]:
    """按天对齐天气预报：给每一天配一条，预报覆盖不到的日期留 null（前端显示「天气待定」）。"""
    if not destination or not days_out:
        return []
    try:
        casts = geo.weather(destination, days=len(days_out) + 2)
    except Exception:
        casts = []
    if not casts:
        return []
    by_date = {c.get("date"): c for c in casts}
    return [by_date.get(d.get("date")) for d in days_out]


def _timeline_to_spots(timeline: List[dict]) -> List[dict]:
    """把 final_plan 的 timeline[] 转回自研 spots[]（编辑保存用）。"""
    spots: List[dict] = []
    for it in timeline or []:
        loc = it.get("location") or {}
        if it.get("type") == "attraction":
            spots.append({
                "name": it.get("name"),
                "category": it.get("category") or "景点",
                "address": it.get("address"),
                "lon": loc.get("lng"),
                "lat": loc.get("lat"),
                "arrive": it.get("start_time"),
                "leave": it.get("end_time"),
                "tip": it.get("note") or it.get("tip"),
                "source": "amap",
            })
        else:
            spots.append({
                "name": it.get("name"),
                "category": "美食",
                "address": it.get("address") or it.get("addr"),
                "lon": loc.get("lng"),
                "lat": loc.get("lat"),
                "arrive": None,
                "leave": None,
                "tip": it.get("reason"),
                "source": "meal",
            })
    return spots


def _history_item(row, data: dict) -> dict:
    days = data.get("days") or []
    start_date = row["start_date"] or (days[0].get("date") if days else None)
    end_date = None
    if start_date and days:
        try:
            y, m, dd = map(int, start_date.split("-")[:3])
            end_date = (_date(y, m, dd) + timedelta(days=len(days) - 1)).isoformat()
        except Exception:
            end_date = None
    return {
        "id": row["id"],
        "title": row["title"],
        "destination": data.get("destination") or row["destination"],
        "start_date": start_date,
        "end_date": end_date,
        "duration_days": len(days) or row["days_count"],
        "version": 1,
        "created_at": row["created_at"],
        "updated_at": row["created_at"],
    }


def _day_km(timeline: List[dict]) -> float:
    pts = [
        it for it in timeline
        if it.get("location") and it["location"].get("lat") and it["location"].get("lng")
    ]
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        total += haversine(
            a["location"]["lng"], a["location"]["lat"],
            b["location"]["lng"], b["location"]["lat"],
        )
    return round(total, 1)


def _optimize_timeline(timeline: List[dict]) -> List[dict]:
    """对当天所有带坐标的点做最近邻重排，作为「优化当天路线」。"""
    with_loc = [it for it in timeline if it.get("location") and it["location"].get("lat")]
    without_loc = [it for it in timeline if not (it.get("location") and it["location"].get("lat"))]
    if len(with_loc) <= 2:
        return timeline
    ordered = [with_loc[0]]
    remaining = with_loc[1:]
    while remaining:
        cur = ordered[-1]
        remaining.sort(key=lambda s: haversine(
            cur["location"]["lng"], cur["location"]["lat"],
            s["location"]["lng"], s["location"]["lat"],
        ))
        ordered.append(remaining.pop(0))
    return ordered + without_loc


# ── Profile（旅行画像 = 记忆快照）────────────────────
def _fact_out(row) -> dict:
    try:
        scope_key = json.loads(row["scope_key"])
    except Exception:
        scope_key = {}
    return {
        "id": row["id"],
        "category": row["category"],
        "polarity": row["polarity"],
        "value_text": row["value_text"],
        "scope_type": row["scope_type"],
        "scope_key": scope_key,
        "source_kind": row["source_kind"],
        "sensitivity": row["sensitivity"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _bump_revision(c, uid: str) -> None:
    c.execute(
        "INSERT INTO profile_meta(user_id, revision) VALUES(?, 2) "
        "ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1",
        (uid,),
    )


@router.get("/api/profile")
def profile(uid: str = Depends(current_user)):
    with connection() as c:
        meta = c.execute(
            "SELECT revision FROM profile_meta WHERE user_id=?", (uid,)
        ).fetchone()
        rows = c.execute(
            "SELECT * FROM memories WHERE user_id=? ORDER BY created_at DESC", (uid,)
        ).fetchall()
        trip_count = c.execute(
            "SELECT COUNT(*) AS n FROM journeys WHERE owner_id=?", (uid,)
        ).fetchone()["n"]
    facts = [_fact_out(r) for r in rows]
    return {
        "revision": (meta["revision"] if meta else 1),
        "active_facts": [f for f in facts if f["status"] == "active"],
        "candidate_facts": [f for f in facts if f["status"] == "candidate"],
        "trip_count": trip_count,
    }


# ── History ─────────────────────────────────────────
@router.get("/api/history")
def history(limit: int = 0, cursor: Optional[str] = None,
            uid: str = Depends(current_user)):
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except (TypeError, ValueError):
            offset = 0
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM journeys WHERE owner_id=? AND COALESCE(status,'') != 'deleted' ORDER BY created_at DESC", (uid,)
        ).fetchall()
    items = []
    for r in rows:
        try:
            data = json.loads(r["plan_json"])
        except Exception:
            continue
        items.append(_history_item(r, data))

    total = len(items)
    if limit and limit > 0:
        page = items[offset:offset + limit]
        nxt = offset + limit
        next_cursor = str(nxt) if nxt < total else None
    else:
        page, next_cursor = items, None
    return {"items": page, "next_cursor": next_cursor, "total": total}


@router.delete("/api/history/{item_id}")
def delete_history_item(item_id: str, uid: str = Depends(current_user)):
    _fetch_journey(item_id, uid)
    with connection() as c:
        c.execute(
            "UPDATE journeys SET status='deleted' WHERE id=? AND owner_id=?", (item_id, uid)
        )
    return {"status": "deleted"}


@router.get("/api/history/{item_id}")
def history_item(item_id: str, uid: str = Depends(current_user)):
    row = _fetch_journey(item_id, uid)
    data = json.loads(row["plan_json"])
    return {
        "id": row["id"],
        "plan": _to_final_plan(data, row["id"]),
        "created_at": row["created_at"],
    }


@router.post("/api/history/{item_id}/tips")
def retry_tips(item_id: str, uid: str = Depends(current_user)):
    _fetch_journey(item_id, uid)
    return {"status": "succeeded"}


@router.get("/api/history/{item_id}/tips/stream")
def stream_tips(item_id: str, uid: str = Depends(current_user)):
    def gen():
        yield f"event: custom\ndata: {json.dumps({'kind': 'tips.completed', 'status': 'succeeded'})}\n\n"
        yield "event: end\ndata: {\"status\": \"succeeded\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── Plan 操作（详情页）──────────────────────────────
@router.put("/api/plan/{plan_id}/timeline")
def save_timeline(plan_id: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    row = _fetch_journey(plan_id, uid)
    data = json.loads(row["plan_json"])
    old_days = data.get("days", [])
    new_days = []
    for i, d in enumerate(body.get("days") or []):
        day_no = d.get("day") or (i + 1)
        old = next((x for x in old_days if x.get("index") == day_no), old_days[i] if i < len(old_days) else {})
        new_days.append({
            "index": day_no,
            "date": old.get("date"),
            "theme": old.get("theme"),
            "spots": _timeline_to_spots(d.get("timeline") or []),
        })
    data["days"] = new_days
    data["days_count"] = len(new_days)
    _save_plan_json(plan_id, data)
    return {"plan": _to_final_plan(data, plan_id)}


@router.put("/api/plan/{plan_id}/metadata")
def save_metadata(plan_id: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    row = _fetch_journey(plan_id, uid)
    data = json.loads(row["plan_json"])
    for key in ("hotel", "notes", "day_themes"):
        if key in body:
            data[key] = body[key]
    _save_plan_json(plan_id, data)
    return {"ok": True, "plan": _to_final_plan(data, plan_id)}


@router.post("/api/plan/optimize_day")
def optimize_day(body: Dict[str, Any], uid: str = Depends(current_user)):
    plan_id = body.get("plan_id")
    day_no = body.get("day")
    row = _fetch_journey(plan_id, uid)
    data = json.loads(row["plan_json"])
    days = data.get("days", [])
    idx = next((i for i, d in enumerate(days) if d.get("index") == day_no), None)
    if idx is None:
        raise HTTPException(400, "找不到对应天数")
    fp = _to_final_plan(data, plan_id)
    timeline = fp["days"][idx]["timeline"]
    original_km = _day_km(timeline)
    optimized = _optimize_timeline(timeline)
    optimized_km = _day_km(optimized)
    improved = optimized_km < original_km - 0.05
    # 回写
    days[idx]["spots"] = _timeline_to_spots(optimized)
    data["days"] = days
    _save_plan_json(plan_id, data)
    return {
        "improved": improved,
        "original_km": original_km,
        "optimized_km": optimized_km,
    }


@router.post("/api/plan/revert_day")
def revert_day(body: Dict[str, Any], uid: str = Depends(current_user)):
    plan_id = body.get("plan_id")
    day_no = body.get("day")
    original_timeline = body.get("original_timeline") or []
    row = _fetch_journey(plan_id, uid)
    data = json.loads(row["plan_json"])
    days = data.get("days", [])
    idx = next((i for i, d in enumerate(days) if d.get("index") == day_no), None)
    if idx is None:
        raise HTTPException(400, "找不到对应天数")
    days[idx]["spots"] = _timeline_to_spots(original_timeline)
    data["days"] = days
    _save_plan_json(plan_id, data)
    return {"ok": True, "improved": False}


# ── 发现页：热门景点 ──────────────────────────────────────
# 公开内容，不鉴权（未登录也要能看）。
# 选品来自 discover.CURATED 人工清单（高德 sortrule=weight 是通用 POI 权重，不是
# 游客热度，实测成都首位是金融城双子塔、西安首位是城市运动公园，已弃用）；
# 评分 / 营业时间 / 照片 每次走高德按名字实时取，1 小时缓存。
@router.get("/api/discover/hot")
def discover_hot(limit: int = 12):
    items = discover.hot_places(limit=limit)
    return {
        "items": items,
        "count": len(items),
        "cities": sorted({i["city"] for i in items}),
        "month": time.localtime().tm_mon,
    }


# ── POI / 路线（高德 Web Service，缺 Key 时用内置样例兜底）──
@router.get("/api/poi/search")
def poi_search(city: str = "", kw: str = "", kind: str = "attraction", uid: str = Depends(current_user)):
    return {"results": geo.text_search(city, kw, kind)}


@router.get("/api/poi/nearby")
def poi_nearby(lat: float = 0, lng: float = 0, type: str = "", radius: int = 1500, uid: str = Depends(current_user)):
    return {"results": geo.nearby(lat, lng, type or "风景名胜", radius)}


@router.get("/api/route/walking")
def route_walking(
    origin_lng: float = 0, origin_lat: float = 0,
    dest_lng: float = 0, dest_lat: float = 0,
    uid: str = Depends(current_user),
):
    coords = geo.walking_route((origin_lng, origin_lat), (dest_lng, dest_lat))
    return {"coords": coords}


@router.get("/api/route/day")
def route_day(pts: str = "", uid: str = Depends(current_user)):
    """一整天的路线，沿真实道路（近段步行、远段驾车），pts 形如「lng,lat;lng,lat;...」。"""
    points = []
    for chunk in (pts or "").split(";"):
        parts = chunk.split(",")
        if len(parts) >= 2:
            try:
                points.append({"lng": float(parts[0]), "lat": float(parts[1])})
            except ValueError:
                continue
    return {"coords": geo.day_route(points)}


# ── 静态底图（高德 Web 服务，无需 JS API Key）────────────
# 返回底图参数，前端用墨卡托自行投影画编号点和路线。
# 不加鉴权：<img>/fetch 直取，内容只是公开底图，密钥不出服务端。
@router.get("/api/map/static")
def map_static(pts: str = "", w: int = 700, h: int = 1000):
    points = []
    for chunk in (pts or "").split(";"):
        parts = chunk.split(",")
        if len(parts) >= 2:
            try:
                points.append({"lng": float(parts[0]), "lat": float(parts[1])})
            except ValueError:
                continue
    meta = geo.static_map_meta(points[:20], w, h)
    if not meta:
        raise HTTPException(404, "静态地图不可用")
    return {**meta, "image": f"/api/map/raw?k={quote(meta['key'])}"}


@router.get("/api/map/raw")
def map_raw(k: str = ""):
    data = geo.static_map_bytes(k)
    if not data:
        raise HTTPException(404, "底图不存在")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── 会话 / 任务运行时 ────────────────────────────────
# 一次对话里的每个请求都是一个 run：后台线程推进阶段、写事件，SSE 只负责把
# 事件按顺序推给前端。这样「规划中/停止/重试/等待你补充」才有真实的对象可操作。
_PRODUCT_STAGE = {
    "intent": "understand", "revision_prepare": "understand",
    "attraction_search": "discover", "revision_search": "discover",
    "time_window": "compose",
    "optimizer": "compose", "revision_apply": "compose",
    "finalize": "polish",
}
_STEP_INDEX = {
    "intent": 0, "weather_lookup": 0, "revision_prepare": 0,
    "attraction_search": 1, "candidate_builder": 1, "revision_search": 1,
    "time_window": 2,
    "optimizer": 2, "quality_gate": 2, "planner": 2, "revision_apply": 2,
    "reviewer": 3, "time_check": 4,
    "meal_search": 5, "spot_tips": 5, "finalize": 5,
}

_cancel_flags: Dict[str, bool] = {}
_briefs: Dict[str, dict] = {}
_conv_brief: Dict[str, str] = {}

_RUN_TERMINAL = ("succeeded", "failed", "cancelled")


def _brief_from_row(row) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "owner_id": row["owner_id"],
        "status": row["status"],
        "data": json.loads(row["data"] or "{}"),
        "missing_fields": json.loads(row["missing_fields"] or "[]"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_brief(bid: str) -> Optional[dict]:
    if not bid:
        return None
    with _lock:
        cached = _briefs.get(bid)
    if cached is not None:
        return cached
    with connection() as c:
        row = c.execute("SELECT * FROM planning_briefs WHERE id=?", (bid,)).fetchone()
    if not row:
        return None
    brief = _brief_from_row(row)
    with _lock:
        _briefs[bid] = brief
    return brief


def _put_brief(brief: dict) -> None:
    with _lock:
        _briefs[brief["id"]] = brief
        _conv_brief[brief["conversation_id"]] = brief["id"]
    with connection() as c:
        c.execute(
            """INSERT INTO planning_briefs(id, conversation_id, owner_id, status, data,
               missing_fields, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data,
                 missing_fields=excluded.missing_fields, updated_at=excluded.updated_at""",
            (
                brief["id"], brief["conversation_id"], brief["owner_id"], brief["status"],
                json.dumps(brief["data"], ensure_ascii=False),
                json.dumps(brief.get("missing_fields") or [], ensure_ascii=False),
                brief["created_at"], brief["updated_at"],
            ),
        )


def _conv_brief_id(cid: str) -> Optional[str]:
    with _lock:
        bid = _conv_brief.get(cid)
    if bid:
        return bid
    with connection() as c:
        row = c.execute(
            "SELECT id FROM planning_briefs WHERE conversation_id=? "
            "AND status IN ('collecting','ready','submitted') "
            "ORDER BY created_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
    if row:
        with _lock:
            _conv_brief[cid] = row["id"]
        return row["id"]
    return None


class _RunCancelled(Exception):
    pass


def _cancelled(rid: str) -> bool:
    with _lock:
        return bool(_cancel_flags.get(rid))


def _emit(rid: str, payload: dict) -> dict:
    """追加一条事件，并顺手把阶段信息同步到 run 上（列表接口要用）。"""
    with _lock:
        bucket = _events.setdefault(rid, [])
        bucket.append({
            "sequence": len(bucket) + 1,
            "kind": "custom",
            "payload": payload,
            "created_at": _now(),
        })
        run = _runs.get(rid)
        if run and payload.get("kind") == "planning_run.progress":
            stage = payload.get("stage") or ""
            run["internal_stage"] = stage
            run["product_stage"] = _PRODUCT_STAGE.get(stage) or run.get("product_stage")
            run["journey_step_index"] = _STEP_INDEX.get(stage, run.get("journey_step_index") or 0)
            run["latest_progress_label"] = payload.get("label") or run.get("latest_progress_label")
    return payload


def _wait(rid: str, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _cancelled(rid):
            raise _RunCancelled()
        time.sleep(0.05)


def _stage(rid: str, stage: str, label: str, seconds: float = 0.35) -> None:
    if _cancelled(rid):
        raise _RunCancelled()
    _emit(rid, {"kind": "planning_run.progress", "stage": stage, "label": label})
    _wait(rid, seconds)


def _terminal(rid: str, status: str, plan_id: Optional[str] = None, message: Optional[str] = None) -> None:
    cid = None
    with _lock:
        run = _runs.get(rid)
        if run:
            run["status"] = status
            run["finished_at"] = _now()
            run["terminal_event_at"] = _now()
            run["pending_interaction"] = None
            if plan_id:
                run["result_itinerary_id"] = plan_id
            if message:
                run["error_public"] = {"message": message}
            cid = run.get("conversation_id")
    if cid and status == "succeeded":
        conv = _get_conv(cid)
        if conv:
            conv["has_unread_completed"] = True
            conv["updated_at"] = _now()
            _put_conv(conv)
    _emit(rid, {"kind": "run.status", "status": status})


def _append_message(cid: str, message: dict) -> dict:
    _put_message(message)
    return message


def _assistant_reply(rid: str, cid: str, content: str,
                     plan_id: Optional[str] = None,
                     artifacts: Optional[list] = None) -> dict:
    message = {
        "id": _id(),
        "role": "assistant",
        "content": content,
        "sequence": _next_seq(cid),
        "created_at": _now(),
        "conversation_id": cid,
        "artifacts": artifacts or [],
    }
    payload = {
        "kind": "chat.message.completed",
        "message_id": message["id"],
        "content": content,
        "sequence": message["sequence"],
        "created_at": message["created_at"],
        "related_run_id": rid,
        "artifacts": message["artifacts"],
    }
    if plan_id:
        payload["related_itinerary_id"] = plan_id
        message["related_itinerary_id"] = plan_id
        conv = _get_conv(cid)
        if conv:
            conv["related_itinerary_id"] = plan_id
            conv["updated_at"] = _now()
            _put_conv(conv)
    _emit(rid, payload)
    _append_message(cid, message)
    return message


def _new_run(rid: str, cid: str, uid: str, *, kind: str = "chat") -> dict:
    now = _now()
    run = {
        "id": rid,
        "kind": kind,
        "conversation_id": cid,
        "owner_id": uid,
        "status": "queued",
        "result_itinerary_id": None,
        "product_stage": None,
        "internal_stage": None,
        "latest_progress_label": "准备开始",
        "request_snapshot": {},
        "created_at": now,
        "queued_at": now,
        "started_at": None,
        "updated_at": now,
        "activity_at": now,
        "finished_at": None,
        "terminal_event_at": None,
        "pending_interaction": None,
    }
    with _lock:
        _runs[rid] = run
        _events[rid] = []
        _cancel_flags[rid] = False
    return run


def _start(rid: str, fn, *args) -> None:
    threading.Thread(target=fn, args=(rid, *args), daemon=True).start()


def _run_view(rid: str) -> dict:
    with _lock:
        return dict(_runs.get(rid) or {})


# ── 旅行画像 → 规划约束 ──────────────────────────────
def _active_taste_texts(uid: str) -> List[str]:
    with connection() as c:
        rows = c.execute(
            "SELECT value_text FROM memories WHERE user_id=? AND status='active' "
            "AND polarity='prefer' ORDER BY created_at DESC LIMIT 12",
            (uid,),
        ).fetchall()
    return [r["value_text"] for r in rows if r["value_text"]]


def _brief_constraints(uid: str) -> List[dict]:
    with connection() as c:
        rows = c.execute(
            "SELECT category, value_text, polarity FROM memories "
            "WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 20",
            (uid,),
        ).fetchall()
    return [
        {"category": r["category"], "value_text": r["value_text"], "polarity": r["polarity"] or "prefer"}
        for r in rows if r["value_text"]
    ]


# ── 规划简报（必要条件确认）─────────────────────────
def _brief_public(brief: dict) -> dict:
    return {
        "id": brief["id"],
        "conversation_id": brief["conversation_id"],
        "status": brief["status"],
        "data": brief["data"],
        "missing_fields": brief.get("missing_fields") or [],
        "created_at": brief["created_at"],
        "updated_at": brief["updated_at"],
    }


def _active_brief(cid: str, statuses=("collecting", "ready")) -> Optional[dict]:
    brief = _get_brief(_conv_brief_id(cid) or "")
    if brief and brief.get("status") in statuses:
        return brief
    return None


def _emit_brief(rid: str, brief: dict) -> None:
    _emit(rid, {
        "kind": f"planning_brief.{brief['status']}",
        "brief_id": brief["id"],
        "status": brief["status"],
        "summary": brief["data"],
        "missing_fields": brief.get("missing_fields") or [],
        "created_at": brief.get("created_at"),
        "updated_at": brief.get("updated_at"),
    })


def _new_brief(cid: str, uid: str) -> dict:
    now = _now()
    brief = {
        "id": _id(),
        "conversation_id": cid,
        "owner_id": uid,
        "status": "collecting",
        "data": {"trip_constraints": _brief_constraints(uid)},
        "missing_fields": ["destination"],
        "created_at": now,
        "updated_at": now,
    }
    _put_brief(brief)
    return brief


def _hydrate_brief_dates(brief: dict) -> None:
    """在已有信息之间做推导：日期 ⇄ 天数。缺的绝不凭空补——必须问用户。"""
    data = brief["data"]
    start = _as_date(data.get("start_date"))
    end = _as_date(data.get("end_date"))
    try:
        days = int(data.get("days") or 0)
    except (TypeError, ValueError):
        days = 0

    if start and end and end >= start:
        data["days"] = (end - start).days + 1
    elif start and days > 0:
        data["end_date"] = (start + timedelta(days=days - 1)).isoformat()
    elif end and days > 0:
        data["start_date"] = (end - timedelta(days=days - 1)).isoformat()
    if start:
        data["start_date"] = start.isoformat()
    if end:
        data["end_date"] = end.isoformat()


def _as_date(value) -> Optional[_date]:
    if not value:
        return None
    try:
        return _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _brief_pitch(brief: dict) -> str:
    """齐全后那段确认话术。"""
    data = brief["data"]
    parts = [f"「{data.get('destination')}」{int(data.get('days') or 2)} 天"]
    if data.get("start_date"):
        parts.append(f"{str(data['start_date'])[5:]} 出发")
    if data.get("transport"):
        parts.append(data["transport"])
    if data.get("companions"):
        parts.append(data["companions"])
    if data.get("attraction_preference"):
        parts.append(f"偏好{data['attraction_preference']}")
    if data.get("habit_preference"):
        parts.append(data["habit_preference"])
    text = "，".join(parts)
    taste = [c.get("value_text") for c in (data.get("trip_constraints") or []) if c.get("value_text")][:2]
    if taste:
        text += "；也带上了你画像里的「" + "、".join(taste) + "」"
    return text + "。确认没问题就点「开始规划」，想调哪里直接说。"


def _brief_missing(brief: dict) -> List[str]:
    """还差什么才够开始正式规划。

    必要条件是「目的地」+「具体日历日期」，天数不等于日期——
    用户只说「三天」但没有出发日期时，仍然是 collecting。
    预算、餐饮、兴趣、节奏等偏好缺席不阻塞，按默认值直接 ready。
    """
    data = brief["data"]
    missing: List[str] = []
    if not data.get("destination"):
        missing.append("destination")
    if not (data.get("start_date") and data.get("end_date")):
        missing.append("dates")
    return missing


def _known_brief(data: dict) -> str:
    bits = []
    if data.get("destination"):
        bits.append(f"目的地{data['destination']}")
    if data.get("days"):
        bits.append(f"{int(data['days'])} 天")
    if data.get("start_date"):
        bits.append(f"{str(data['start_date'])[5:]} 出发")
    if data.get("transport"):
        bits.append(data["transport"])
    if data.get("companions"):
        bits.append(data["companions"])
    if data.get("attraction_preference"):
        bits.append(f"偏好{data['attraction_preference']}")
    return "、".join(bits)


def _fallback_question(missing: List[str], data: dict) -> str:
    if "destination" in missing:
        return "还没定去哪儿。想飞哪座城市？说个地名，我先帮你摸一遍有什么可玩的。"
    dest = data.get("destination") or "这边"
    days = f"{int(data['days'])} 天" if data.get("days") else ""
    head = f"「{dest}」{days}".strip()
    if data.get("transport"):
        head += f"、{data['transport']}"
    if "dates" in missing:
        return f"{head}，记下了。打算哪天出发？给个具体日期，我好按天气和闭馆日来排。"
    return f"{head}，记下了。还有什么要带的偏好？没有的话我就按常规节奏排。"


def _brief_question(brief: dict) -> str:
    """还缺信息时那句追问。让模型说得像人话，失败就用模板兜。"""
    data = brief["data"]
    missing = _brief_missing(brief)
    fallback = _fallback_question(missing, data)
    ask = {"destination": "目的地城市", "dates": "具体出发日期（哪一天到哪一天）"}
    gaps = "、".join(ask[m] for m in missing if m in ask)
    try:
        from .planner.llm import get_llm

        llm = get_llm()
        if not llm:
            return fallback
        answer = llm.complete(
            "你在帮用户规划一趟旅行。已知信息：" + (_known_brief(data) or "无")
            + "。现在只缺：" + gaps + "。"
            "请写出你接下来要说的那一句话：先用半句话确认你听到的内容，再问缺的那一件事。"
            "要求：口语中文、一句话、不超过 45 字、不用列表、不要编造景点或价格、不要客套结尾。",
            max_tokens=80,
        )
    except Exception:  # noqa: BLE001
        return fallback
    text = (answer or "").strip().strip('"').replace("\n", "")
    if not text or len(text) > 60:
        return fallback
    return text


def _merge_brief_patch(brief: dict, patch: dict) -> dict:
    """把模型给出的结构化增量合并进简报（只做合并，不重新理解文本）。"""
    data = brief["data"]
    take = ("destination", "start_date", "end_date", "companions",
            "transport", "budget", "food", "note")
    for key in take:
        value = patch.get(key)
        if value:
            data[key] = value
    if patch.get("days"):
        data["days"] = int(patch["days"])
    if patch.get("pace"):
        data["habit_preference"] = {
            "relaxed": "慢节奏", "balanced": "松紧适中", "tight": "紧凑高效",
        }[patch["pace"]]
        data["pace"] = patch["pace"]

    interests = [str(x).strip() for x in (patch.get("interests") or []) if str(x).strip()]
    if interests:
        existing = [x for x in str(data.get("attraction_preference") or "").split("、") if x]
        merged = existing + [x for x in interests if x not in existing]
        data["attraction_preference"] = "、".join(merged[:6])
        data["interests"] = merged[:6]

    avoid = [str(x).strip() for x in (patch.get("avoid") or []) if str(x).strip()]
    if avoid:
        current = list(data.get("avoid") or [])
        data["avoid"] = current + [x for x in avoid if x not in current]

    explicit = list(data.get("explicit_constraints") or [])
    for kind, polarity in (("attraction", "require"), ):
        for text in interests:
            explicit.append({"category": kind, "polarity": polarity,
                             "value_text": text, "source": "conversation"})
    for text in avoid:
        explicit.append({"category": "attraction", "polarity": "avoid",
                         "value_text": text, "source": "conversation"})
    data["explicit_constraints"] = explicit[-12:]

    _hydrate_brief_dates(brief)
    missing = _brief_missing(brief)
    brief["missing_fields"] = missing
    brief["status"] = "collecting" if missing else "ready"
    brief["updated_at"] = _now()
    _put_brief(brief)
    return brief


# ── Chat Bridge：把存储与运行时能力暴露给主 Agent 工具 ──
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_HISTORY_LIMIT = 8
# 这些约束类别语义上是单值，模型换个说法重写时直接覆盖，不并列
_SINGLE_VALUE_CATEGORIES = {
    "companion_context", "travel_pace", "budget_style", "accommodation_preference",
}
_DURABLE_BRIEF_FIELDS = {
    "destination", "start_date", "end_date", "days", "trip_focus", "budget",
    "trip_budget", "companions", "attraction_preference", "food_preference",
    "habit_preference", "trip_constraints", "excluded_memory_fact_ids",
}


def _recent_history(cid: str, limit: int = _HISTORY_LIMIT) -> List[dict]:
    msgs = [m for m in _get_messages(cid) if m.get("content")]
    return [{"role": m.get("role"), "content": m.get("content")}
            for m in msgs[-(limit + 1):-1]]


def _memory_revision(uid: str) -> int:
    with connection() as c:
        row = c.execute(
            "SELECT revision FROM profile_meta WHERE user_id=?", (uid,)
        ).fetchone()
    return int(row["revision"]) if row else 1


def _memory_snapshot(uid: str) -> List[dict]:
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE user_id=? ORDER BY category, created_at",
            (uid,),
        ).fetchall()
    out = []
    for row in rows:
        try:
            scope = json.loads(row["scope_key"] or "{}")
        except (TypeError, ValueError):
            scope = {}
        out.append({
            "id": row["id"], "category": row["category"], "polarity": row["polarity"],
            "value_text": row["value_text"], "scope_type": row["scope_type"],
            "scope_key": scope, "status": row["status"],
            "source_kind": row["source_kind"],
            "source_conversation_id": None, "evidence_sequences": [],
        })
    return out


def _brief_context(brief: Optional[dict]) -> Optional[dict]:
    if not brief:
        return None
    return {
        "id": brief["id"], "status": brief["status"], "data": brief["data"],
        "missing_fields": brief["missing_fields"],
    }


def _latest_brief(uid: str, cid: str) -> Optional[dict]:
    with connection() as c:
        row = c.execute(
            "SELECT * FROM planning_briefs WHERE conversation_id=? AND owner_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (cid, uid),
        ).fetchone()
    return _brief_from_row(row) if row else None


def _days_between(start: Optional[_date], end: Optional[_date]) -> Optional[int]:
    if start and end and end >= start:
        return (end - start).days + 1
    return None


def _normalize_brief_dates(data: dict) -> None:
    start = _as_date(data.get("start_date"))
    end = _as_date(data.get("end_date"))
    days = data.get("days")
    if start and end is None and isinstance(days, int) and days >= 1:
        data["end_date"] = (start + timedelta(days=days - 1)).isoformat()
        return
    if start and end:
        data["days"] = (end - start).days + 1


def _apply_brief_patch_data(brief: dict, patch: dict) -> Optional[dict]:
    """把 PlanningBriefPatch 增量合并进简报；返回 (brief, error)。"""
    patch = dict(patch or {})
    if not patch:
        return brief
    data = dict(brief["data"])

    # 模型偶尔把「同一个月日」的年份写错（例如把 2026-10-21 说成 2025-10-21）。
    # 月日相同而年份不同时，保留简报里已有的年份，避免日期被整体改签一年。
    for key in ("start_date", "end_date"):
        incoming_text, current_text = patch.get(key), data.get(key)
        if not incoming_text or not current_text:
            continue
        incoming_date, current_date = _as_date(incoming_text), _as_date(current_text)
        if (incoming_date and current_date
                and (incoming_date.month, incoming_date.day)
                == (current_date.month, current_date.day)
                and incoming_date.year != current_date.year):
            patch[key] = current_date.isoformat()

    remove_ids = {str(x) for x in (patch.pop("remove_trip_constraint_ids", None) or [])}
    incoming = patch.pop("trip_constraints", None) or []

    def _ck(item: dict):
        return (item.get("category"), str(item.get("value_text") or "").casefold(),
                item.get("polarity"))

    # 没有 id 的约束也必须留住：模型经常只给 category/value_text/polarity。
    # 之前的实现按 id 建字典，id 缺失的旧约束会被整批丢掉，于是下一轮「同行」等信息会消失。
    merged: List[dict] = [
        dict(item) for item in (data.get("trip_constraints") or [])
        if str(item.get("id")) not in remove_ids
    ]
    used_ids = {str(item.get("id")) for item in merged if item.get("id")}

    def _next_id() -> str:
        n = len(used_ids) + 1
        while f"c{n}" in used_ids:
            n += 1
        used_ids.add(f"c{n}")
        return f"c{n}"

    for item in incoming:
        item = dict(item)
        item_id = str(item.get("id") or "")
        if item_id:
            hit = next((i for i, cur in enumerate(merged)
                        if str(cur.get("id") or "") == item_id), None)
            if hit is not None:
                merged[hit] = item
                continue
        key = _ck(item)
        duplicate = next((i for i, cur in enumerate(merged) if _ck(cur) == key), None)
        if duplicate is None and item.get("category") in _SINGLE_VALUE_CATEGORIES:
            # 同行、节奏这类事实只会有一个有效值，模型换个说法再写一次时应该覆盖而不是并列
            duplicate = next((i for i, cur in enumerate(merged)
                              if cur.get("category") == item.get("category")
                              and cur.get("polarity") == item.get("polarity")), None)
            if duplicate is not None:
                item.setdefault("id", merged[duplicate].get("id") or item_id or _next_id())
        item.setdefault("id", item_id or _next_id())
        if duplicate is not None:
            merged[duplicate] = item
        else:
            merged.append(item)

    excluded = set(data.get("excluded_memory_fact_ids") or [])
    excluded.update(patch.pop("excluded_memory_fact_ids", None) or [])
    excluded.difference_update(patch.pop("restored_memory_fact_ids", None) or [])

    data.update({k: v for k, v in patch.items() if v is not None})
    if merged or "trip_constraints" in data:
        data["trip_constraints"] = merged
    if excluded:
        data["excluded_memory_fact_ids"] = sorted(excluded)

    start = _as_date(data.get("start_date"))
    end = _as_date(data.get("end_date"))
    if start and end and end < start:
        return None
    _normalize_brief_dates(data)

    brief["data"] = {k: v for k, v in data.items() if k in _DURABLE_BRIEF_FIELDS}
    missing = required_brief_fields(brief["data"])
    brief["missing_fields"] = missing
    brief["status"] = "collecting" if missing else "ready"
    brief["updated_at"] = _now()
    _put_brief(brief)
    return brief


def _brief_interests(data: dict) -> List[str]:
    """把结构化约束翻译成检索关键词，不再靠词表反推。"""
    out: List[str] = []
    for item in data.get("trip_constraints") or []:
        if item.get("polarity") == "avoid":
            continue
        if item.get("category") in ("attraction_preference", "other_travel_preference"):
            text = str(item.get("value_text") or "").strip()
            if text:
                out.append(text)
    if data.get("attraction_preference"):
        out.append(str(data["attraction_preference"]))
    focus = data.get("trip_focus")
    if focus == "food_first" and "美食" not in out:
        out.append("美食")
    seen, unique = set(), []
    for word in out:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique[:6]


def _brief_avoid(data: dict) -> List[str]:
    return [
        str(item.get("value_text") or "").strip()
        for item in data.get("trip_constraints") or []
        if item.get("polarity") == "avoid" and str(item.get("value_text") or "").strip()
    ][:8]


class _ChatBridge:
    """主 Agent 工具访问业务状态的唯一通道（全部 owner-scoped）。"""

    # ── 记忆 ──────────────────────────────────────
    def memory_snapshot(self, uid: str):
        return _memory_revision(uid), _memory_snapshot(uid)

    # ── 简报 ──────────────────────────────────────
    def active_brief(self, uid: str, cid: str) -> Optional[dict]:
        brief = _get_brief(_conv_brief_id(cid) or "")
        if brief and brief.get("owner_id") == uid:
            return brief
        return None

    def latest_brief(self, uid: str, cid: str) -> Optional[dict]:
        return _latest_brief(uid, cid)

    def brief_context(self, brief: Optional[dict]) -> Optional[dict]:
        return _brief_context(brief)

    async def apply_brief_patch(self, uid: str, cid: str, rid: str, patch: dict):
        from .chat.models import PlanningBriefPatch

        try:
            parsed = PlanningBriefPatch.model_validate(patch)
        except Exception:  # noqa: BLE001
            return None, "旅行需求字段不符合要求，请检查日期、天数和约束。"
        payload = parsed.model_dump(exclude_none=True, exclude_unset=True)
        if not payload:
            return None, "我还需要一点旅行信息，才能继续整理这趟行程。"
        brief = self.active_brief(uid, cid) or _new_brief(cid, uid)
        updated = _apply_brief_patch_data(brief, payload)
        if updated is None:
            return None, "结束日期不能早于开始日期，请确认日期范围。"
        _emit_brief(rid, updated)
        return updated, None

    # ── 行程 ──────────────────────────────────────
    def list_itineraries(self, uid: str) -> List[dict]:
        with connection() as c:
            rows = c.execute(
                "SELECT id, title, destination, start_date, days_count, plan_json, created_at "
                "FROM journeys WHERE owner_id=? ORDER BY created_at DESC, id DESC",
                (uid,),
            ).fetchall()
        out = []
        for row in rows:
            try:
                plan = json.loads(row["plan_json"] or "{}")
            except (TypeError, ValueError):
                plan = {}
            start = _as_date(row["start_date"])
            duration = int(row["days_count"] or 0) or None
            end = (start + timedelta(days=duration - 1)).isoformat() if start and duration else None
            out.append({
                "itinerary_id": row["id"], "root_id": row["id"], "version": 1,
                "destination": row["destination"] or plan.get("destination") or "",
                "duration_days": duration, "start_date": row["start_date"],
                "end_date": end, "created_at": row["created_at"] or "",
                "is_modified": bool(plan.get("revision_of")),
                "highlights": self._highlights(plan), "plan": plan,
                "title": row["title"],
            })
        return out

    def itinerary_detail(self, uid: str, iid: str) -> Optional[dict]:
        for item in self.list_itineraries(uid):
            if item["itinerary_id"] == iid:
                return item
        return None

    def itinerary_summary(self, uid: str, iid: Optional[str]) -> Optional[dict]:
        item = self.itinerary_detail(uid, iid) if iid else None
        if not item:
            return None
        return {k: item.get(k) for k in (
            "itinerary_id", "destination", "start_date", "end_date", "duration_days", "version",
        )}

    @staticmethod
    def _highlights(plan: dict) -> List[str]:
        values: List[str] = []
        for day in plan.get("days") or []:
            for spot in day.get("spots") or []:
                name = spot.get("name")
                if name and spot.get("source") != "meal" and str(name) not in values:
                    values.append(str(name)[:60])
                if len(values) >= 4:
                    return values
        return values

    # ── 任务 ──────────────────────────────────────
    def active_runs(self, uid: str, cid: str) -> List[dict]:
        with _lock:
            return [
                {"id": run["id"], "kind": run.get("kind"), "status": run.get("status"),
                 "conversation_id": cid}
                for run in _runs.values()
                if run.get("conversation_id") == cid and run.get("status") in (
                    "queued", "running", "waiting_user")
            ]

    def conversation_targets(self, uid: str, cid: str) -> List[dict]:
        out = []
        with _lock:
            runs = [dict(r) for r in _runs.values() if r.get("conversation_id") == cid]
        for run in runs:
            if run.get("kind") not in ("travel_plan", "revision"):
                continue
            snapshot = run.get("request_snapshot") or {}
            out.append({
                "run_id": run["id"],
                "itinerary_id": run.get("result_itinerary_id") or snapshot.get("related_itinerary_id"),
                "kind": run.get("kind"), "status": run.get("status"),
                "destination": snapshot.get("destination", ""),
            })
        for item in self.list_itineraries(uid)[:5]:
            if not any(t["itinerary_id"] == item["itinerary_id"] for t in out):
                out.append({
                    "run_id": None, "itinerary_id": item["itinerary_id"],
                    "kind": "saved", "status": "succeeded",
                    "destination": item["destination"],
                })
        return out

    def run_result_itinerary(self, rid: str) -> Optional[str]:
        return _run_view(rid).get("result_itinerary_id")

    async def run_planner(self, uid: str, cid: str, rid: str, brief_id: str) -> Optional[str]:
        brief = _get_brief(brief_id)
        if not brief:
            return None
        return await asyncio.to_thread(_execute_planning, rid, uid, brief)

    async def run_revision(self, uid: str, cid: str, rid: str, iid: str,
                           notes: str):
        return await asyncio.to_thread(_execute_revision, rid, uid, iid, notes)

    async def control_run(self, uid: str, cid: str, rid: str, run_id: str, action: str):
        from .chat.models import DialogueDecision

        decision = DialogueDecision(
            intent="run_control", reply="任务状态已更新。",
            target={"run_id": run_id}, run_action=action,
        )
        return await asyncio.to_thread(_execute_run_control, cid, uid, decision)

    # ── Agent 输入 ────────────────────────────────
    def agent_context(self, run: dict):
        from .chat.models import MainAgentContext

        snapshot = run.get("request_snapshot") or {}
        return MainAgentContext(
            user_id=run["owner_id"], conversation_id=run["conversation_id"],
            chat_run_id=run["id"], memory_revision=_memory_revision(run["owner_id"]),
            current_message=str(snapshot.get("text") or ""),
            related_run_id=snapshot.get("related_run_id"),
            related_itinerary_id=snapshot.get("related_itinerary_id"),
        )

    def agent_messages(self, run: dict) -> List:
        from .chat.prompts import main_agent_messages

        uid, cid = run["owner_id"], run["conversation_id"]
        return main_agent_messages({
            "today": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "timezone": "Asia/Shanghai",
            "profile_snapshot": _memory_snapshot(uid),
            "profile_revision": _memory_revision(uid),
            "history": _recent_history(cid),
            "current_message": str((run.get("request_snapshot") or {}).get("text") or ""),
        })


_bridge = _ChatBridge()
_runner = None


def _agent_runner():
    global _runner
    if _runner is None:
        from .chat.runtime import AgentRunner

        _runner = AgentRunner(_bridge)
    return _runner


def _execute_planning(rid: str, uid: str, brief: dict) -> Optional[str]:
    """在当前 Chat Run 内执行 Planner task：写行程、发进度，但不结束 Run。"""
    data = (brief or {}).get("data") or {}
    destination = data.get("destination")
    days = data.get("days")
    start_date = data.get("start_date")
    if not days and start_date and data.get("end_date"):
        days = _days_between(_as_date(start_date), _as_date(data.get("end_date")))
    need = " ".join(str(x) for x in (
        destination,
        f"{days}天" if days else "",
        start_date,
        "旅行行程规划",
    ) if x)
    stage_plan = {
        "weather_lookup": ("结合旅行偏好与天气", 0.35),
        "attraction_search": (
            f"检索「{destination}」的景点" if destination else "检索景点池", 0.6),
        "candidate_builder": ("筛选候选景点", 0.35),
        "time_window": ("判断每个景点的最佳到访时段", 0.45),
        "optimizer": ("编排行程与路线", 0.5),
        "reviewer": ("评审路线连贯性与节奏", 0.45),
        "meal_search": ("挑选本地特色餐馆", 0.5),
        "time_check": ("核查开放时间、强度与冲突", 0.45),
        "finalize": ("补充餐饮建议与出行贴士", 0.35),
    }

    def on_stage(key: str) -> None:
        label, seconds = stage_plan.get(key, (key, 0.2))
        _stage(rid, key, label, seconds)

    result = run_plan(
        need=need,
        tastes=_active_taste_texts(uid),
        destination=destination,
        start_date=start_date,
        days=days,
        interests=_brief_interests(data),
        avoid=data.get("avoid") or _brief_avoid(data),
        stage=on_stage,
    )
    plan_id = _persist_plan(uid, result, need)
    if brief:
        brief["status"] = "submitted"
        brief["updated_at"] = _now()
        _put_brief(brief)
        # 让前端立刻收起「规划确认卡」，否则它会一直挂到下次刷新
        _emit_brief(rid, brief)
    _emit(rid, {
        "kind": "planning.itinerary_created",
        "itinerary_id": plan_id,
        "destination": result.get("destination"),
    })
    with _lock:
        run = _runs.get(rid)
        if run:
            run["result_itinerary_id"] = plan_id
            snapshot = dict(run.get("request_snapshot") or {})
            snapshot["destination"] = result.get("destination")
            snapshot["related_itinerary_id"] = plan_id
            run["request_snapshot"] = snapshot
    return plan_id


def _execute_revision(rid: str, uid: str, iid: str, notes: str):
    """在当前 Chat Run 内执行 Revision task：返回 (itinerary_id, concern)。"""
    instruction = _strip_reference_prefix(notes)
    _stage(rid, "revision_prepare", "对照当前行程理解这次修改", 0.4)
    try:
        row = _fetch_journey(iid, uid)
    except HTTPException:
        row = None
    if not row:
        return None, {
            "question": "这份行程已经找不到了，要重新生成一份吗？",
            "missing_fields": ["modification_notes"],
            "input_schema": {"type": "string", "minLength": 1},
        }
    data = json.loads(row["plan_json"])
    _stage(rid, "planner", "对照当前行程理解这次修改", 0.3)
    updated, _changes, clarify = revision.revise(
        data, instruction, data.get("destination") or "")
    if clarify:
        return None, {
            "question": clarify.get("question") or "请补充要修改的内容",
            "missing_fields": clarify.get("missing_fields") or ["instruction"],
            "input_schema": {"type": "string", "minLength": 1},
        }
    _stage(rid, "revision_apply", "改写当天路线与时间", 0.45)
    _save_plan_json(iid, updated)
    _stage(rid, "finalize", "同步地图与提示", 0.25)
    _emit(rid, {
        "kind": "planning.itinerary_created",
        "itinerary_id": iid,
        "destination": updated.get("destination"),
    })
    with _lock:
        run = _runs.get(rid)
        if run:
            run["result_itinerary_id"] = iid
            snapshot = dict(run.get("request_snapshot") or {})
            snapshot["destination"] = updated.get("destination")
            snapshot["related_itinerary_id"] = iid
            run["request_snapshot"] = snapshot
    return iid, None


def _active_run_of(cid: str) -> Optional[dict]:
    with _lock:
        for run in _runs.values():
            if run.get("conversation_id") != cid:
                continue
            if run.get("status") in ("queued", "running", "waiting_user"):
                return dict(run)
    return None


def _execute_run_control(cid: str, uid: str, decision) -> tuple:
    target_id = decision.target.run_id if decision.target else None
    if not target_id:
        active = _active_run_of(cid)
        target_id = active["id"] if active else None
    if not target_id:
        return None, "我没有找到可以操作的任务。"
    target = _run_view(target_id)
    if not target or target.get("owner_id") != uid:
        return None, "这个任务不可用，请重新选择。"
    if decision.requires_confirmation:
        return None, "请在对应任务卡片上确认这项操作，避免误影响正在进行的规划。"
    if decision.run_action == "cancel":
        if target.get("status") in _RUN_TERMINAL:
            return None, "这个任务已经结束，不能再停止。"
        with _lock:
            _cancel_flags[target_id] = True
        return {"id": target_id, "status": "cancelled"}, None
    if target.get("status") not in ("failed", "cancelled"):
        return None, "只有已停止或未完成的任务可以重新尝试。"
    with _lock:
        _cancel_flags[target_id] = False
        run = _runs.get(target_id)
        if run:
            run["status"] = "queued"
    retry = dict(target.get("_retry") or {})
    if retry.get("kind") == "chat":
        _start(target_id, _worker_chat, cid, uid, retry.get("content") or "")
    elif retry.get("kind") == "revision":
        _start(target_id, _worker_revision, cid, uid, retry.get("content") or "", retry.get("plan_id"))
    elif retry.get("kind") == "plan":
        _start(target_id, _worker_plan, cid, uid, retry.get("content") or "", retry.get("brief_id"))
    return {"id": target_id, "status": "retrying"}, None


# ── Chat Run：ReAct 主 Agent ─────────────────────────
# 所有 Chat Run 共用一个常驻事件循环：LangChain 的异步 HTTP 客户端会把连接池
# 绑在首个循环上，每轮 asyncio.run() 新建循环会导致 "Event loop is closed"。
_agent_loop: Optional[asyncio.AbstractEventLoop] = None
_agent_loop_guard = threading.Lock()


def _loop_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _agent_event_loop() -> asyncio.AbstractEventLoop:
    global _agent_loop
    with _agent_loop_guard:
        if _agent_loop is None or _agent_loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=_loop_main, args=(loop,),
                             name="agent-loop", daemon=True).start()
            _agent_loop = loop
    return _agent_loop


def _asyncio_run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("already inside a running loop")
    return asyncio.run_coroutine_threadsafe(coro, _agent_event_loop()).result()


def _stream_custom(rid: str, payload: dict) -> None:
    _emit(rid, payload)


def _chat_worker_impl(rid: str, cid: str, uid: str, content: str,
                      resume_value=None) -> None:
    run = _run_view(rid)
    if not run:
        return
    try:
        if resume_value is None:
            _emit(rid, {"kind": "run.status", "status": "running"})
        runner = _agent_runner()

        buffer = {"text": ""}

        def on_delta(delta: str) -> None:
            buffer["text"] += delta
            _emit(rid, {"kind": "messages", "delta": delta,
                        "message_id": f"assistant:{rid}"})

        async def go():
            return await runner.run_turn(
                run,
                messages=_bridge.agent_messages(run) if resume_value is None else None,
                resume_value=resume_value,
                on_custom=lambda payload: _stream_custom(rid, payload),
                on_delta=on_delta,
            )

        result = _asyncio_run(go())

        if _cancelled(rid):
            _terminal(rid, "cancelled")
            return

        if result["interrupted"]:
            interaction = result["interaction"] or {}
            interaction["kind"] = "run.waiting_user"
            interaction["input_schema"] = interaction.get("input_schema") or {
                "type": "string", "minLength": 1}
            with _lock:
                target = _runs.get(rid)
                if target:
                    target["status"] = "waiting_user"
                    target["pending_interaction"] = interaction
                    target["activity_at"] = _now()
                    target["latest_progress_label"] = "等你回复"
            _emit(rid, interaction)
            _emit(rid, {"kind": "run.status", "status": "waiting_user"})
            return

        plan_id = _run_view(rid).get("result_itinerary_id")
        text = (result.get("final_text") or "").strip() or buffer["text"].strip()
        if not text:
            text = "我已经处理了这条旅行消息。"
        from .chat.runtime import collect_artifacts

        _assistant_reply(rid, cid, text, plan_id=plan_id,
                         artifacts=collect_artifacts(result.get("messages") or []))
        _terminal(rid, "succeeded", plan_id)
    except _RunCancelled:
        _terminal(rid, "cancelled")
    except Exception as error:  # noqa: BLE001
        logger.exception("chat run failed")
        _assistant_reply(rid, cid, _chat_error_text(error))
        _terminal(rid, "failed", message=_chat_error_text(error))


def _chat_error_text(error: Exception) -> str:
    name = type(error).__name__
    text = str(error)
    if "Connection" in name or "Timeout" in name or "ConnectError" in text:
        return "暂时连不上 AI 服务，检查网络或代理后再试一次。"
    if "Authentication" in name or "401" in text:
        return "AI 服务认证失败，请检查 DeepSeek Key 配置。"
    return "这条消息暂时没有处理成功，请重试。"


def _worker_chat(rid: str, cid: str, uid: str, content: str) -> None:
    with _lock:
        run = _runs.get(rid)
        if run:
            run["_retry"] = {"kind": "chat", "content": content}
            run["request_snapshot"] = {
                "text": content,
                "related_run_id": run.get("related_run_id"),
                "related_itinerary_id": run.get("related_itinerary_id"),
            }
    _chat_worker_impl(rid, cid, uid, content)


def _resume_chat(rid: str, cid: str, uid: str, value: str) -> None:
    _chat_worker_impl(rid, cid, uid, "", resume_value=value)


def _start_planning(cid: str, uid: str, rid: str, content: str,
                    brief: Optional[dict]) -> None:
    with _lock:
        run = _runs.get(rid)
        if run:
            run["status"] = "running"
            run["started_at"] = _now()
            run["request_snapshot"] = {"destination": (brief or {}).get("data", {}).get("destination")} if brief else {}
            run["_retry"] = {"kind": "plan", "content": content, "brief_id": brief["id"] if brief else None}
    if brief:
        brief["status"] = "submitted"
        brief["updated_at"] = _now()
        _put_brief(brief)
        _emit_brief(rid, brief)
    _start(rid, _worker_plan, cid, uid, content, brief["id"] if brief else None)


def _default_start() -> str:
    """没有明确出发日期时，默认排到明天，保证每天都有可展示的日期。"""
    return (_date.today() + timedelta(days=1)).isoformat()


def _fill_dates(days: List[dict], start_date: Optional[str]) -> Optional[str]:
    """把 start_date 落到每一天上，缺失时按顺序顺延。"""
    if not days:
        return start_date
    base = start_date or days[0].get("date") or _default_start()
    try:
        y, m, d = (int(x) for x in str(base).split("-")[:3])
        cursor = _date(y, m, d)
    except Exception:
        base = _default_start()
        cursor = _date.fromisoformat(base)
    for day in days:
        day["date"] = cursor.isoformat()
        cursor += timedelta(days=1)
    return base


def _persist_plan(uid: str, result: dict, raw_need: str) -> str:
    plan_id = _id()
    now = _now()
    days = result.get("days") or []
    start_date = _fill_dates(days, days[0].get("date") if days else None)
    store = dict(result)
    store["start_date"] = start_date
    with connection() as c:
        c.execute(
            """INSERT INTO journeys(id, owner_id, title, destination, start_date, days_count,
               raw_need, status, plan_json, created_at)
               VALUES(:id,:owner_id,:title,:destination,:start_date,:days_count,
                      :raw_need,:status,:plan_json,:created_at)""",
            {
                "id": plan_id,
                "owner_id": uid,
                "title": f"{result.get('destination') or '旅行'}自由行",
                "destination": result.get("destination") or "旅行",
                "start_date": start_date,
                "days_count": result.get("days_count") or len(days),
                "raw_need": raw_need,
                "status": "planned",
                "plan_json": json.dumps(store, ensure_ascii=False),
                "created_at": now,
            },
        )
    return plan_id


def _plan_summary(result: dict) -> str:
    days = result.get("days") or []
    destination = result.get("destination") or "目的地"
    spots = sum(len([s for s in (d.get("spots") or []) if s.get("source") != "meal"])
                for d in days)
    first = ""
    if days and days[0].get("spots"):
        names = [s.get("name") for s in days[0]["spots"][:3]
                 if s.get("name") and s.get("source") != "meal"][:2]
        if names:
            first = f"第一天从{'、'.join(names)}开始。"
    return (
        f"「{destination}」{len(days)} 天行程排好了，一共 {spots} 处景点。{first}"
        "右侧可以逐天看路线和地图，想改哪一段直接说就行。"
    )


def _worker_plan(rid: str, cid: str, uid: str, content: str, brief_id: Optional[str]) -> None:
    try:
        _emit(rid, {"kind": "run.status", "status": "running"})
        brief = _get_brief(brief_id) if brief_id else None
        data = (brief or {}).get("data") or {}
        destination = data.get("destination")
        _stage(rid, "weather_lookup", "查看目的地天气", 0.35)
        _stage(rid, "attraction_search",
               f"搜索「{destination}」的景点" if destination else "搜集目的地信息", 0.5)
        _stage(rid, "candidate_builder", "筛选候选景点", 0.35)
        result = run_plan(
            need=content,
            tastes=_active_taste_texts(uid),
            destination=destination,
            start_date=data.get("start_date"),
            days=data.get("days"),
            interests=_brief_interests(data),
            avoid=data.get("avoid") or [],
        )
        _stage(rid, "optimizer", "求解最优路线", 0.4)
        _stage(rid, "quality_gate", "校验行程质量", 0.25)
        plan_id = _persist_plan(uid, result, content)
        if brief:
            brief["status"] = "submitted"
            brief["updated_at"] = _now()
            _put_brief(brief)
        _stage(rid, "spot_tips", "完善旅行细节", 0.25)
        _emit(rid, {
            "kind": "planning.itinerary_created",
            "itinerary_id": plan_id,
            "destination": result.get("destination"),
        })
        with _lock:
            run = _runs.get(rid)
            if run:
                run["request_snapshot"] = {"destination": result.get("destination")}
        _assistant_reply(rid, cid, _plan_summary(result), plan_id=plan_id)
        _terminal(rid, "succeeded", plan_id)
    except _RunCancelled:
        _terminal(rid, "cancelled")
    except ValueError as error:
        _assistant_reply(rid, cid, f"这次没排成：{error}")
        _terminal(rid, "failed", message=str(error))
    except Exception:  # noqa: BLE001
        _assistant_reply(rid, cid, "规划过程中出了点问题，换个说法再试一次？")
        _terminal(rid, "failed", message="规划失败，请重试")


# ── 对话改行程 ──────────────────────────────────────
def _strip_reference_prefix(text: str) -> str:
    return re.sub(r"^@[^\n]*\n", "", (text or "").strip())


def _worker_revision(rid: str, cid: str, uid: str, content: str, plan_id: str) -> None:
    try:
        _emit(rid, {"kind": "run.status", "status": "running"})
        instruction = _strip_reference_prefix(content)
        _stage(rid, "revision_prepare", "对照当前行程理解这次修改", 0.45)
        row = _fetch_journey(plan_id, uid)
        data = json.loads(row["plan_json"])
        _stage(rid, "planner", "对照当前行程理解这次修改", 0.35)
        updated, changes, clarify = revision.revise(data, instruction, data.get("destination") or "")
        if clarify:
            interaction = {
                "interaction_id": _id(),
                "kind": "run.waiting_user",
                "question": clarify.get("question") or "请补充要修改的内容",
                "input_schema": {"format": "text"},
                "missing_fields": clarify.get("missing_fields") or ["instruction"],
            }
            with _lock:
                run = _runs.get(rid)
                if run:
                    run["status"] = "waiting_user"
                    run["pending_interaction"] = interaction
                    run["activity_at"] = _now()
                    run["_pending"] = {"kind": "revision", "plan_id": plan_id, "content": instruction}
            _emit(rid, interaction)
            _emit(rid, {"kind": "run.status", "status": "waiting_user"})
            return
        _stage(rid, "revision_apply", "改写当天路线与时间", 0.5)
        _save_plan_json(plan_id, updated)
        with _lock:
            run = _runs.get(rid)
            if run:
                run["request_snapshot"] = {"destination": updated.get("destination")}
        _stage(rid, "finalize", "同步地图与提示", 0.3)
        _emit(rid, {
            "kind": "planning.itinerary_created",
            "itinerary_id": plan_id,
            "destination": updated.get("destination"),
        })
        _assistant_reply(
            rid, cid,
            "改好了：" + "；".join(changes) + "。右侧路线和地图已经同步。",
            plan_id=plan_id,
        )
        _terminal(rid, "succeeded", plan_id)
    except _RunCancelled:
        _terminal(rid, "cancelled")
    except HTTPException as error:
        _assistant_reply(rid, cid, f"这份行程找不到了：{error.detail}")
        _terminal(rid, "failed", message=str(error.detail))
    except Exception:  # noqa: BLE001
        _assistant_reply(rid, cid, "这次修改没成功，再说一次具体想改哪里？")
        _terminal(rid, "failed", message="修改失败，请重试")


# ── 关于当前行程的问答（不改动行程）──────────────────
# ── Conversations ───────────────────────────────────
def _conversation_view(conv: dict) -> dict:
    cid = conv["id"]
    with _lock:
        runs = [r for r in _runs.values() if r.get("conversation_id") == cid]
        unread = bool(conv.get("has_unread_completed"))
    brief = _get_brief(_conv_brief_id(cid) or "")
    return {
        **conv,
        "has_waiting_user": any(r.get("status") == "waiting_user" for r in runs),
        "has_active_planning": any(r.get("status") in ("queued", "running") for r in runs),
        "has_ready_brief": bool(brief and brief.get("status") == "ready"),
        "has_unread_completed": unread,
    }


def _conversation(cid: str, uid: str) -> dict:
    conv = _get_conv(cid)
    if not conv or conv.get("owner_id") != uid:
        raise HTTPException(404, "对话不存在")
    return conv


@router.get("/api/conversations")
def list_conversations(related_itinerary_id: Optional[str] = None, uid: str = Depends(current_user)):
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM conversations WHERE owner_id=? ORDER BY created_at DESC", (uid,)
        ).fetchall()
    convs = []
    for row in rows:
        conv = _get_conv(row["id"]) or _conv_from_row(row)
        if related_itinerary_id and conv.get("related_itinerary_id") != related_itinerary_id:
            continue
        convs.append(conv)
    return [_conversation_view(c) for c in convs]


@router.post("/api/conversations")
def create_conversation(body: Dict[str, Any], uid: str = Depends(current_user)):
    cid = _id()
    now = _now()
    conv = {
        "id": cid,
        "title": (body.get("title") or "新的旅行对话").strip() or "新的旅行对话",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "owner_id": uid,
        "has_waiting_user": False,
        "has_ready_brief": False,
        "has_active_planning": False,
        "has_unread_completed": False,
        "finalization_status": None,
        "related_itinerary_id": body.get("related_itinerary_id"),
    }
    with _lock:
        _messages[cid] = []
    _put_conv(conv)
    return conv


@router.post("/api/conversations/{cid}/view")
def mark_viewed(cid: str, uid: str = Depends(current_user)):
    conv = _conversation(cid, uid)
    conv["has_unread_completed"] = False
    return _conversation_view(conv)


@router.get("/api/conversations/{cid}/messages")
def get_messages(cid: str, after_sequence: int = 0, uid: str = Depends(current_user)):
    _conversation(cid, uid)
    msgs = list(_get_messages(cid))
    return [m for m in msgs if m["sequence"] > after_sequence]


@router.post("/api/conversations/{cid}/messages")
def submit_message(cid: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    conv = _conversation(cid, uid)
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "消息不能为空")
    now = _now()
    user_msg = {
        "id": _id(),
        "role": "user",
        "content": content,
        "sequence": _next_seq(cid),
        "created_at": now,
        "conversation_id": cid,
        "artifacts": [],
    }
    _append_message(cid, user_msg)
    conv["updated_at"] = now
    if not conv.get("title") or conv.get("title") == "新的旅行对话":
        conv["title"] = content[:24]
    if body.get("related_itinerary_id"):
        conv["related_itinerary_id"] = body["related_itinerary_id"]
    _put_conv(conv)

    rid = _id()
    run = _new_run(rid, cid, uid, kind="chat")
    run["related_run_id"] = body.get("related_run_id") or None
    run["related_itinerary_id"] = (
        body.get("related_itinerary_id") or conv.get("related_itinerary_id") or None
    )
    run["request_snapshot"] = {
        "text": content,
        "related_run_id": run["related_run_id"],
        "related_itinerary_id": run["related_itinerary_id"],
    }
    # 提交入口不做任何文本预路由：一律先落消息、开 Chat Run，
    # 由 ReAct 主 Agent 通过受控工具决定动作，模型不直接改状态。
    _start(rid, _worker_chat, cid, uid, content)
    return {"message": user_msg, "run": _public_run(run)}


@router.post("/api/conversations/{cid}/compress")
def compress(cid: str, uid: str = Depends(current_user)):
    """把较早的消息压成一条摘要，保留最近 6 条原文。"""
    _conversation(cid, uid)
    msgs = list(_get_messages(cid))
    if len(msgs) <= 8:
        return {"ok": True, "compressed": False, "kept": len(msgs)}
    head, tail = msgs[:-6], msgs[-6:]
    summary = {
        "id": _id(),
        "role": "assistant",
        "content": f"（前 {len(head)} 条对话已压缩为摘要）中提到："
                   + "；".join((m.get("content") or "")[:18] for m in head[-3:]),
        "sequence": float(head[-1]["sequence"]) + 0.5,
        "created_at": _now(),
        "conversation_id": cid,
        "artifacts": [],
    }
    with connection() as c:
        c.execute(
            "DELETE FROM conversation_messages WHERE conversation_id=? AND sequence < ?",
            (cid, tail[0]["sequence"]),
        )
    _put_message(summary)
    with _lock:
        _messages[cid] = [summary] + tail
    conv = _get_conv(cid)
    if conv:
        conv["updated_at"] = _now()
        _put_conv(conv)
    return {"ok": True, "compressed": True, "kept": 7}


_MEMORY_RULES = [
    (r"不吃辣|怕辣|不能吃辣|忌辣", "dietary_requirement", "avoid", "不吃辣"),
    (r"吃素|素食", "dietary_requirement", "require", "素食"),
    (r"带孩子|带娃|亲子", "companion_context", "prefer", "带孩子出行"),
    (r"不想早起|赖床|自然醒", "schedule_preference", "avoid", "不想早起"),
    (r"慢节奏|别太满|不要太赶|悠闲", "travel_pace", "prefer", "慢节奏，一天别排太满"),
    (r"紧凑|赶一点|多去几个", "travel_pace", "prefer", "节奏紧凑，想多看几处"),
    (r"预算有限|省钱|穷游", "budget_style", "prefer", "预算有限"),
    (r"喜欢博物馆|爱看展", "attraction_preference", "prefer", "喜欢博物馆和展览"),
    (r"喜欢自然|山水|徒步", "attraction_preference", "prefer", "偏爱自然山水"),
    (r"喜欢历史|古迹", "attraction_preference", "prefer", "对历史古迹感兴趣"),
    (r"不爱走路|腿脚不方便", "accessibility_need", "require", "少走路"),
]


def _extract_memories(cid: str, uid: str) -> int:
    texts = [m.get("content") or "" for m in _get_messages(cid) if m.get("role") == "user"]
    found: List[dict] = []
    seen = set()
    for text in texts:
        for pattern, category, polarity, value in _MEMORY_RULES:
            if value in seen or not re.search(pattern, text):
                continue
            seen.add(value)
            found.append({"category": category, "polarity": polarity, "value": value})
    if not found:
        return 0
    now = _now()
    created = 0
    with connection() as c:
        existing = {
            r["value_text"] for r in c.execute(
                "SELECT value_text FROM memories WHERE user_id=?", (uid,)
            ).fetchall()
        }
        for item in found:
            if item["value"] in existing:
                continue
            c.execute(
                """INSERT INTO memories(id, user_id, category, polarity, value_text,
                   scope_type, scope_key, status, source_kind, sensitivity, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _id(), uid, item["category"], item["polarity"], item["value"],
                    "global", "{}", "candidate", "conversation", "normal", now, now,
                ),
            )
            created += 1
        if created:
            _bump_revision(c, uid)
    return created


@router.post("/api/conversations/{cid}/archive")
def archive(cid: str, uid: str = Depends(current_user)):
    conv = _conversation(cid, uid)
    created = _extract_memories(cid, uid)
    conv["status"] = "archived"
    conv["updated_at"] = _now()
    conv["finalization_status"] = "succeeded"
    conv["memory_created"] = created
    _put_conv(conv)
    return _conversation_view(conv)


@router.post("/api/conversations/{cid}/memory/retry")
def memory_retry(cid: str, uid: str = Depends(current_user)):
    conv = _conversation(cid, uid)
    created = _extract_memories(cid, uid)
    conv["finalization_status"] = "succeeded"
    conv["memory_error_code"] = None
    conv["memory_created"] = created
    _put_conv(conv)
    return _conversation_view(conv)


@router.get("/api/conversations/{cid}/planning-brief")
def planning_brief(cid: str, uid: str = Depends(current_user)):
    _conversation(cid, uid)
    brief = _active_brief(cid, ("collecting", "ready", "submitted"))
    return _brief_public(brief) if brief else None


def _brief_or_404(bid: str, uid: str) -> dict:
    brief = _get_brief(bid)
    if not brief or brief.get("owner_id") != uid:
        raise HTTPException(404, "简报不存在")
    return brief


@router.patch("/api/planning-briefs/{bid}")
def update_brief(bid: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    brief = _brief_or_404(bid, uid)
    patch = body.get("data") if isinstance(body.get("data"), dict) else body
    for key in ("destination", "days", "start_date", "end_date", "hotel", "notes"):
        if key in patch and patch[key] not in (None, ""):
            brief["data"][key] = patch[key]
    if isinstance(patch.get("trip_constraints"), list):
        brief["data"]["trip_constraints"] = patch["trip_constraints"]
    # 用户手动编辑后，readiness 由服务端统一重算，不由前端声明
    _hydrate_brief_dates(brief)
    brief["missing_fields"] = _brief_missing(brief)
    brief["status"] = "collecting" if brief["missing_fields"] else "ready"
    brief["updated_at"] = _now()
    _put_brief(brief)
    return _brief_public(brief)


@router.post("/api/planning-briefs/{bid}/submit")
def submit_brief(bid: str, uid: str = Depends(current_user)):
    brief = _brief_or_404(bid, uid)
    if brief.get("status") == "submitted":
        # 幂等：同一份简报只允许创建一个正式 PlanningRun
        return {"brief": _brief_public(brief), "run": None}
    missing = _brief_missing(brief)
    if missing:
        label = {"destination": "还缺目的地", "dates": "还缺具体出发日期"}
        raise HTTPException(400, label.get(missing[0], "信息还不完整"))
    brief["status"] = "submitted"
    brief["updated_at"] = _now()
    _put_brief(brief)
    cid = brief["conversation_id"]
    rid = _id()
    _new_run(rid, cid, uid)
    need = brief["data"].get("destination") or "旅行"
    if brief["data"].get("days"):
        need = f"{need} {brief['data']['days']} 天"
    _start_planning(cid, uid, rid, need, brief)
    return {"brief": _brief_public(brief), "run": _run_view(rid)}


@router.post("/api/planning-briefs/{bid}/discard")
def discard_brief(bid: str, uid: str = Depends(current_user)):
    brief = _brief_or_404(bid, uid)
    brief["status"] = "discarded"
    brief["updated_at"] = _now()
    _put_brief(brief)
    with _lock:
        if _conv_brief.get(brief["conversation_id"]) == bid:
            _conv_brief.pop(brief["conversation_id"], None)
    return _brief_public(brief)


@router.post("/api/planning-briefs/{bid}/memory/refresh")
def refresh_brief_memory(bid: str, uid: str = Depends(current_user)):
    brief = _brief_or_404(bid, uid)
    brief["data"]["trip_constraints"] = _brief_constraints(uid)
    brief["updated_at"] = _now()
    return _brief_public(brief)


# ── Runs ────────────────────────────────────────────
@router.get("/api/runs")
def list_runs(conversation_id: Optional[str] = None, active_only: bool = False, uid: str = Depends(current_user)):
    with _lock:
        runs = [dict(r) for r in _runs.values() if r.get("owner_id") == uid]
    if conversation_id:
        runs = [r for r in runs if r.get("conversation_id") == conversation_id]
    if active_only:
        runs = [r for r in runs if r.get("status") in ("queued", "running", "waiting_user")]
    return [_public_run(r) for r in runs]


def _public_run(run: dict) -> dict:
    out = {k: v for k, v in run.items() if not k.startswith("_")}
    # chat 任务在真正进入排程阶段前不暴露 journey_step_index，
    # 前端据此显示「漫程正在思考」气泡，而不是空的规划进度时间轴。
    if out.get("journey_step_index") is None:
        out.pop("journey_step_index", None)
    return out


@router.post("/api/runs")
def create_run(body: Dict[str, Any], uid: str = Depends(current_user)):
    rid = body.get("id") or _id()
    cid = body.get("conversation_id") or ""
    run = _new_run(rid, cid, uid, kind=body.get("kind") or "chat")
    with _lock:
        run["status"] = "succeeded"
        run["result_itinerary_id"] = body.get("related_itinerary_id")
        run["finished_at"] = _now()
        run["terminal_event_at"] = _now()
    _emit(rid, {"kind": "run.status", "status": "succeeded"})
    return _public_run(run)


@router.get("/api/runs/{rid}")
def get_run(rid: str, uid: str = Depends(current_user)):
    with _lock:
        run = _runs.get(rid)
    if not run or run.get("owner_id") != uid:
        raise HTTPException(404, "任务不存在")
    return _public_run(run)


@router.get("/api/runs/{rid}/events")
def get_run_events(rid: str, after_seq: int = 0, uid: str = Depends(current_user)):
    with _lock:
        run = _runs.get(rid)
        evs = list(_events.get(rid, []))
    if not run or run.get("owner_id") != uid:
        raise HTTPException(404, "任务不存在")
    return [e for e in evs if e["sequence"] > after_seq]


@router.get("/api/runs/{rid}/stream")
def stream_run(rid: str, after_seq: int = 0, uid: str = Depends(current_user)):
    with _lock:
        run0 = _runs.get(rid)
        if rid not in _runs:
            raise HTTPException(404, "任务不存在")
        if run0.get("owner_id") != uid:
            raise HTTPException(404, "任务不存在")

    async def gen():
        sent = after_seq
        idle = 0
        status = "queued"
        while True:
            with _lock:
                evs = list(_events.get(rid, []))
                run = dict(_runs.get(rid) or {})
            for event in evs:
                if event["sequence"] <= sent:
                    continue
                sent = event["sequence"]
                yield (
                    f"event: {event['kind']}\n"
                    f"id: {event['sequence']}\n"
                    f"data: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
                )
            status = run.get("status") or "queued"
            if status in _RUN_TERMINAL or status == "waiting_user":
                with _lock:
                    total = len(_events.get(rid, []))
                if sent >= total:
                    break
            idle += 1
            if idle > 600:  # 约 1 分钟后放弃，避免连接悬挂
                break
            await asyncio.sleep(0.12)
        yield f"event: end\ndata: {json.dumps({'status': status}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.post("/api/runs/{rid}/cancel")
def cancel_run(rid: str, uid: str = Depends(current_user)):
    with _lock:
        run = _runs.get(rid)
    if not run or run.get("owner_id") != uid:
        raise HTTPException(404, "任务不存在")
    if run.get("status") in _RUN_TERMINAL:
        return _public_run(run)
    with _lock:
        _cancel_flags[rid] = True
        if run.get("status") == "queued":
            run["status"] = "cancelled"
            run["finished_at"] = _now()
            run["terminal_event_at"] = _now()
    if run.get("status") == "waiting_user":
        _terminal(rid, "cancelled")
    return _public_run(_run_view(rid))


@router.post("/api/runs/{rid}/retry")
def retry_run(rid: str, uid: str = Depends(current_user)):
    with _lock:
        run = _runs.get(rid)
        retry = dict((run or {}).get("_retry") or {})
    if not run or run.get("owner_id") != uid:
        raise HTTPException(404, "任务不存在")
    if not retry:
        raise HTTPException(400, "这个任务不能重试")
    cid = run["conversation_id"]
    with _lock:
        _events[rid] = []
        _cancel_flags[rid] = False
        run.update({
            "status": "running",
            "result_itinerary_id": None,
            "pending_interaction": None,
            "error_public": None,
            "started_at": _now(),
            "finished_at": None,
            "terminal_event_at": None,
            "journey_step_index": 0,
            "product_stage": None,
            "internal_stage": None,
        })
        # chat 重试仍然先回到「思考中」，只有真正进入排程才会出现进度时间轴
        if retry.get("kind") == "chat":
            run.pop("journey_step_index", None)
    if retry.get("kind") == "chat":
        _start(rid, _worker_chat, cid, uid, retry.get("content") or "")
    elif retry.get("kind") == "revision":
        _start(rid, _worker_revision, cid, uid, retry.get("content") or "", retry.get("plan_id"))
    else:
        brief = _get_brief(retry.get("brief_id") or "") if retry.get("brief_id") else None
        _start(rid, _worker_plan, cid, uid, retry.get("content") or "", brief["id"] if brief else None)
    return _public_run(_run_view(rid))


@router.post("/api/runs/{rid}/resume")
def resume_run(rid: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    with _lock:
        run = _runs.get(rid)
    if not run or run.get("owner_id") != uid:
        raise HTTPException(404, "任务不存在")
    if run.get("status") != "waiting_user":
        raise HTTPException(400, "这个任务没有在等待补充")
    interaction = run.get("pending_interaction") or {}
    if body.get("interaction_id") != interaction.get("interaction_id"):
        raise HTTPException(409, "问题已更新，请刷新后重试")
    value = (body.get("value") or "").strip()
    if not value:
        raise HTTPException(400, "请填写补充信息")

    cid = run["conversation_id"]
    message = _append_message(cid, {
        "id": _id(),
        "role": "user",
        "content": value,
        "sequence": _next_seq(cid),
        "created_at": _now(),
        "conversation_id": cid,
        "artifacts": [],
    })
    pending = dict(run.get("_pending") or {})
    with _lock:
        run["status"] = "running"
        run["pending_interaction"] = None
        run["activity_at"] = _now()
        _cancel_flags[rid] = False
        _events[rid] = []
    combined = f"{pending.get('content') or ''} {value}".strip()
    if pending.get("kind") == "revision" and pending.get("plan_id"):
        _start(rid, _worker_revision, cid, uid, combined, pending["plan_id"])
    elif run.get("kind") == "chat" or pending.get("kind") == "chat":
        # Chat Run 的中断由 LangGraph interrupt 产生，用 Command(resume=...) 续跑同一个 Run
        _start(rid, _resume_chat, cid, uid, value)
    else:
        _start(rid, _worker_plan, cid, uid, combined, None)
    return {**_public_run(_run_view(rid)), "accepted_message": message}


# ── Memories（画像页增删改）─────────────────────────
def _memory_row(mid: str, uid: str):
    with connection() as c:
        row = c.execute(
            "SELECT * FROM memories WHERE id=? AND user_id=?", (mid, uid)
        ).fetchone()
    if not row:
        raise HTTPException(404, "记忆不存在")
    return row


@router.get("/api/memories")
def list_memories(uid: str = Depends(current_user)):
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE user_id=? ORDER BY created_at DESC", (uid,)
        ).fetchall()
    return [_fact_out(r) for r in rows]


@router.post("/api/memories")
def create_memory(body: Dict[str, Any], uid: str = Depends(current_user)):
    value_text = (body.get("value_text") or "").strip()
    if not value_text:
        raise HTTPException(400, "记忆内容不能为空")
    mid = _id()
    now = _now()
    with connection() as c:
        c.execute(
            """INSERT INTO memories(id, user_id, category, polarity, value_text,
               scope_type, scope_key, status, source_kind, sensitivity, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mid, uid,
                body.get("category") or "other_travel_preference",
                body.get("polarity") or "prefer",
                value_text,
                body.get("scope_type") or "global",
                json.dumps(body.get("scope_key") or {}, ensure_ascii=False),
                "active",
                "manual",
                "normal",
                now, now,
            ),
        )
        _bump_revision(c, uid)
    return _fact_out(_memory_row(mid, uid))


@router.patch("/api/memories/{mid}")
def update_memory(mid: str, body: Dict[str, Any], uid: str = Depends(current_user)):
    _memory_row(mid, uid)
    sets, vals = [], []
    for key in ("category", "polarity", "scope_type"):
        if body.get(key):
            sets.append(f"{key}=?")
            vals.append(body[key])
    value_text = (body.get("value_text") or "").strip()
    if value_text:
        sets.append("value_text=?")
        vals.append(value_text)
    if "scope_key" in body or "scope_type" in body:
        sets.append("scope_key=?")
        vals.append(json.dumps(body.get("scope_key") or {}, ensure_ascii=False))
    if not sets:
        return _fact_out(_memory_row(mid, uid))
    sets.append("updated_at=?")
    vals.append(_now())
    vals += [mid, uid]
    with connection() as c:
        c.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        _bump_revision(c, uid)
    return _fact_out(_memory_row(mid, uid))


@router.post("/api/memories/{mid}/approve")
def approve_memory(mid: str, uid: str = Depends(current_user)):
    _memory_row(mid, uid)
    with connection() as c:
        c.execute(
            "UPDATE memories SET status='active', updated_at=? WHERE id=? AND user_id=?",
            (_now(), mid, uid),
        )
        _bump_revision(c, uid)
    return _fact_out(_memory_row(mid, uid))


@router.delete("/api/memories/{mid}")
def delete_memory(mid: str, uid: str = Depends(current_user)):
    with connection() as c:
        cur = c.execute(
            "DELETE FROM memories WHERE id=? AND user_id=?", (mid, uid)
        )
        if cur.rowcount:
            _bump_revision(c, uid)
    return {"ok": True}
