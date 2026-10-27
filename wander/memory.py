"""旅行偏好（tastes）：可查看、可增量更新、可遗忘。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .db import connection
from .schemas import TasteItem, TasteUpsert
from .security import current_user

router = APIRouter(prefix="/api/tastes", tags=["tastes"])


@router.get("", response_model=list[TasteItem])
def list_tastes(uid: str = Depends(current_user)):
    with connection() as c:
        rows = c.execute(
            "SELECT category, value, note FROM tastes WHERE user_id=?", (uid,)
        ).fetchall()
    return [TasteItem(category=r["category"], value=r["value"], note=r["note"]) for r in rows]


@router.put("", response_model=list[TasteItem])
def upsert_tastes(body: TasteUpsert, uid: str = Depends(current_user)):
    now = datetime.now(timezone.utc).isoformat()
    with connection() as c:
        for it in body.items:
            c.execute(
                """INSERT INTO tastes(id, user_id, category, value, note, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(user_id, category, value) DO UPDATE SET note=excluded.note""",
                (uuid.uuid4().hex, uid, it.category, it.value, it.note, now),
            )
        rows = c.execute(
            "SELECT category, value, note FROM tastes WHERE user_id=?", (uid,)
        ).fetchall()
    return [TasteItem(category=r["category"], value=r["value"], note=r["note"]) for r in rows]


@router.delete("/{category}/{value}", response_model=list[TasteItem])
def forget_taste(category: str, value: str, uid: str = Depends(current_user)):
    with connection() as c:
        c.execute(
            "DELETE FROM tastes WHERE user_id=? AND category=? AND value=?",
            (uid, category, value),
        )
        rows = c.execute(
            "SELECT category, value, note FROM tastes WHERE user_id=?", (uid,)
        ).fetchall()
    return [TasteItem(category=r["category"], value=r["value"], note=r["note"]) for r in rows]
