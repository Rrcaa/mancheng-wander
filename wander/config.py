"""漫程 · Wander — 服务端配置。

所有可调项来自环境变量；也支持项目根目录的 .env（零依赖解析）。
无 LICENSE 约束，纯自研骨架。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根（wander/）


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取：KEY=VALUE，忽略注释与空行，不覆盖已有环境变量。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 某些沙箱会对已存在的目录拦截 mkdir，忽略即可
    pass

# —— 存储 ——
DB_PATH = os.getenv("WANDER_DB", str(DATA_DIR / "wander.db"))

# —— 地图供应商（默认走内置样例，填了 AMAP_API_KEY 才请求真实接口）——
AMAP_KEY = os.getenv("AMAP_API_KEY", "")
AMAP_JS_KEY = os.getenv("AMAP_JS_KEY", "")
AMAP_JS_SEC = os.getenv("AMAP_JS_SECURITY_CODE", "")

# —— 大模型供应商 ——
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DOUBAO_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "doubao-pro-4k")

# —— 鉴权 ——
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me-please-rotate-to-32b+")
JWT_ALGO = "HS256"
TOKEN_EXPIRE_H = int(os.getenv("TOKEN_EXPIRE_H", "168"))

# —— 运行 ——
REDIS_URL = os.getenv("REDIS_URL", "")
PORT = int(os.getenv("PORT", "8765"))

BRAND = os.getenv("BRAND_NAME", "漫程")
BRAND_EN = os.getenv("BRAND_EN", "Wander")
