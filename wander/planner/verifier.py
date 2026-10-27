"""质量校验：不依赖 LLM 的确定性检查。

返回 warning 列表（非致命），供前端提示与后续迭代。
"""
from __future__ import annotations

from typing import List

from .requirements import Requirement


def _is_meal(spot: dict) -> bool:
    """餐食已经不是「午餐」占位了，名字可能是真实餐馆，只能靠 source/type 判。"""
    return (spot.get("source") == "meal"
            or spot.get("type") in ("lunch", "dinner")
            or str(spot.get("name") or "") in ("午餐", "晚餐"))


def verify(plan: List[dict], req: Requirement) -> List[str]:
    warns: List[str] = []
    total = sum(len(d["spots"]) for d in plan)
    if total == 0:
        warns.append("未检索到可用景点，请检查目的地或补充关键词")
        return warns

    real_spots = [s for d in plan for s in d["spots"] if not _is_meal(s)]
    for d in plan:
        cnt = len([s for s in d["spots"] if not _is_meal(s)])
        if cnt > 5:
            warns.append(f"第{d['index']}天安排了 {cnt} 个景点，节奏偏赶，可考虑精简")
        if cnt == 0:
            warns.append(f"第{d['index']}天没有安排景点")

    if req.interests:
        covered = {s["category"] for s in real_spots}
        missing = [i for i in req.interests if i not in covered and i != "美食"]
        if missing:
            warns.append(f"目的地周边这类偏好不多：{', '.join(missing)}，已按现有景点安排")

    if req.pace == "relaxed" and total / max(len(plan), 1) > 3:
        warns.append("你倾向轻松节奏，但日均景点偏多，已按原需求保留")

    return warns
