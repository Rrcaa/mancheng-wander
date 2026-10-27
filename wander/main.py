"""FastAPI 应用入口。

路由：
  POST /api/auth/*      — 注册 / 登录
  GET  /api/tastes      — 旅行偏好
  POST /api/journeys/plan — 规划
  GET  /api/journeys    — 历史行程
  GET  /api/journeys/:id— 行程详情
  PATCH /api/journeys/:id— 编辑保存
  GET  /api/health      — 健康检查
  GET  /api/config      — 前端地图 Key
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import AMAP_JS_KEY, AMAP_JS_SEC, BRAND, BRAND_EN, PORT, BASE_DIR
from .db import init_db
from . import identity, memory, trips, compat

init_db()

app = FastAPI(title=f"{BRAND} · AI 旅行规划", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(identity.router)
app.include_router(memory.router)
app.include_router(trips.router)
app.include_router(compat.router)


@app.middleware("http")
async def _no_cache(request: Request, call_next):
    """开发阶段禁用缓存：静态资源改动后浏览器立即生效，无需手动强刷。"""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.on_event("startup")
def _warm_discover_cache():
    """发现页要跑 40+ 次高德查询，启动时先在后台预热，用户点进「发现」时就是秒开。

    失败不影响启动（无 Key 时会退化成内置样例数据）。
    """
    import threading

    from .planner import discover

    threading.Thread(target=discover.hot_places, kwargs={"limit": 24, "wait": True}, daemon=True).start()


@app.get("/api/health")
def health():
    return {"status": "ok", "brand": BRAND, "brand_en": BRAND_EN}


@app.get("/api/config")
def client_config():
    from .config import AMAP_KEY
    from .planner.llm import llm_status

    return {
        "amap_js_key": AMAP_JS_KEY,
        "amap_js_security_code": AMAP_JS_SEC,
        "amap_web_key": bool(AMAP_KEY),
        "llm": llm_status(),
    }


# 前端静态资源（原版 frontend 原样迁移到 static/）
# 注意：必须放在所有 API 路由之后挂载，否则会吞掉 /api/* 请求。
_STATIC_DIR = BASE_DIR / "static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
