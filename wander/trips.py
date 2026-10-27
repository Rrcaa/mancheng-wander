"""行程（journeys）：规划、列表、详情、编辑。

编辑采用「客户端回传整份 days 结构 → 服务端重算并落库」的轻量模式，
对应原项目的手动拖拽保存，但接口与实现均为自研。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from .db import connection
from .schemas import JourneyBrief, PlanOut, PlanRequest
from .security import current_user
from .planner.engine import plan as run_plan

router = APIRouter(prefix="/api/journeys", tags=["journeys"])


def _load_tastes(uid: str) -> list[str]:
    with connection() as c:
        rows = c.execute("SELECT value FROM tastes WHERE user_id=?", (uid,)).fetchall()
    return [r["value"] for r in rows]


@router.post("/plan", response_model=PlanOut)
def create_plan(body: PlanRequest, uid: str = Depends(current_user)):
    tastes = _load_tastes(uid)
    try:
        result = run_plan(need=body.need, tastes=tastes, destination=body.destination,
                          start_date=body.start_date, days=body.days)
    except ValueError as e:
        raise HTTPException(400, str(e))

    jid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with connection() as c:
        c.execute(
            """INSERT INTO journeys(id, owner_id, title, destination, start_date, days_count,
               raw_need, status, plan_json, created_at)
               VALUES(:id,:owner_id,:title,:destination,:start_date,:days_count,
                      :raw_need,:status,:plan_json,:created_at)""",
            {
                "id": jid, "owner_id": uid,
                "title": f"{result['destination']}自由行",
                "destination": result["destination"],
                "start_date": body.start_date,
                "days_count": result["days_count"],
                "raw_need": body.need,
                "status": "planned",
                "plan_json": json.dumps(result, ensure_ascii=False),
                "created_at": now,
            },
        )
    return PlanOut(journey_id=jid, created_at=now, **_pick(result))


@router.get("", response_model=list[JourneyBrief])
def list_journeys(uid: str = Depends(current_user)):
    with connection() as c:
        rows = c.execute(
            "SELECT id, title, destination, days_count, status, created_at FROM journeys WHERE owner_id=?",
            (uid,),
        ).fetchall()
    return [JourneyBrief(**dict(r)) for r in rows]


@router.get("/{jid}", response_model=PlanOut)
def get_journey(jid: str, uid: str = Depends(current_user)):
    row = _fetch(jid, uid)
    data = json.loads(row["plan_json"])
    return PlanOut(journey_id=row["id"], created_at=row["created_at"], **_pick(data))


@router.patch("/{jid}", response_model=PlanOut)
def update_journey(jid: str, payload: Dict[str, Any], uid: str = Depends(current_user)):
    row = _fetch(jid, uid)
    data = json.loads(row["plan_json"])
    if "days" in payload:
        data["days"] = payload["days"]
    with connection() as c:
        c.execute("UPDATE journeys SET plan_json=? WHERE id=?",
                  (json.dumps(data, ensure_ascii=False), jid))
    return PlanOut(journey_id=row["id"], created_at=row["created_at"], **_pick(data))


def _fetch(jid: str, uid: str):
    with connection() as c:
        row = c.execute("SELECT * FROM journeys WHERE id=? AND owner_id=?", (jid, uid)).fetchone()
    if not row:
        raise HTTPException(404, "行程不存在")
    return row


def _pick(data: dict) -> dict:
    return {k: data[k] for k in ("destination", "days_count", "days", "warnings")}
