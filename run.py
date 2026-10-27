"""启动入口。

用法：
    python run.py
    → http://localhost:8765
"""
import uvicorn

from wander import config

if __name__ == "__main__":
    print(f"✈️  {config.BRAND} · {config.BRAND_EN} 启动中…")
    print(f"    访问：http://localhost:{config.PORT}")
    uvicorn.run("wander.main:app", host="0.0.0.0", port=config.PORT, reload=False)
