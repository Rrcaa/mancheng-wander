"""账号：注册 / 登录。返回 JWT。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from .db import connection
from .schemas import AccountCreate, AuthOut
from .security import hash_password, verify_password, make_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _handle(body: AccountCreate) -> str:
    return (body.handle or body.username or "").strip()


@router.post("/register", response_model=AuthOut)
def register(body: AccountCreate):
    handle = _handle(body)
    if not handle:
        raise HTTPException(422, "请填写用户名")
    uid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with connection() as c:
        exists = c.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()
        if exists:
            raise HTTPException(409, "该用户名已被注册")
        c.execute(
            "INSERT INTO accounts(id, handle, email, pw_hash, created_at) VALUES(?,?,?,?,?)",
            (uid, handle, body.email, hash_password(body.password), now),
        )
    token = make_token(uid)
    return AuthOut(token=token, username=handle, access_token=token)


@router.post("/login", response_model=AuthOut)
def login(body: AccountCreate):
    handle = _handle(body)
    if not handle:
        raise HTTPException(422, "请填写用户名")
    with connection() as c:
        row = c.execute("SELECT id, pw_hash FROM accounts WHERE handle=?", (handle,)).fetchone()
        if row:
            if not verify_password(body.password, row["pw_hash"]):
                raise HTTPException(401, "该用户名已存在，密码不正确")
            uid = row["id"]
        else:
            # 演示模式：登录即注册，任意用户名/密码可直接进入（生产环境请去掉此分支）
            uid = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO accounts(id, handle, email, pw_hash, created_at) VALUES(?,?,?,?,?)",
                (uid, handle, body.email, hash_password(body.password), now),
            )
    token = make_token(uid)
    return AuthOut(token=token, username=handle, access_token=token)
