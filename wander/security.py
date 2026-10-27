"""鉴权：密码哈希 + JWT。无第三方原生依赖（pbkdf2 + PyJWT）。"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException

from .config import JWT_ALGO, JWT_SECRET

_PBKDF2_ROUNDS = 120_000


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return f"{salt}:{dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, dk = stored.split(":")
        nd = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
        return secrets.compare_digest(nd.hex(), dk)
    except Exception:
        return False


def make_token(user_id: str, expire_h: int = 168) -> str:
    exp = int(time.time()) + expire_h * 3600
    return jwt.encode({"sub": user_id, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> Optional[str]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])["sub"]
    except Exception:
        return None


async def current_user(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少访问令牌")
    uid = decode_token(authorization[len("Bearer "):])
    if not uid:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return uid
