"""主 Agent 的确定性工具实现（owner-scoped）。

所有工具都不接受用户 ID，作用域由服务器注入的 `MainAgentContext` 决定；
工具本身不读原始用户消息，也不做自然语言判断。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from langgraph.types import interrupt

from .brief_rules import required_brief_fields, required_input_interrupt
from .models import AgentToolResult, MainAgentContext

MEMORY_CATEGORIES = {
    "attraction_preference", "food_preference", "dietary_requirement",
    "travel_pace", "budget_style", "transport_preference",
    "accommodation_preference", "schedule_preference", "companion_context",
    "accessibility_need", "destination_history", "other_travel_preference",
}
MEMORY_POLARITIES = {"prefer", "avoid", "require", "fact"}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _date_days(start: Any, end: Any) -> Optional[int]:
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
    except (TypeError, ValueError):
        return None


class MainAgentToolService:
    def __init__(self, bridge: Any):
        self.bridge = bridge

    # ── 只读工具 ────────────────────────────────────
    def get_travel_memory(
        self, context: MainAgentContext, *, categories: list[str] | None = None,
        destination: str | None = None, companion: str | None = None,
        polarities: list[str] | None = None, include_provenance: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        chosen_categories = set(categories or MEMORY_CATEGORIES)
        chosen_polarities = set(polarities or MEMORY_POLARITIES)
        if not chosen_categories <= MEMORY_CATEGORIES:
            raise ValueError("invalid memory category")
        if not chosen_polarities <= MEMORY_POLARITIES:
            raise ValueError("invalid memory polarity")
        revision, snapshot = self.bridge.memory_snapshot(context.user_id)
        if int(revision) != context.memory_revision:
            raise ValueError("frozen memory revision mismatch")
        destination_key = _clean_text(destination)
        companion_key = _clean_text(companion)
        facts: list[dict[str, Any]] = []
        for fact in snapshot:
            if fact.get("status") != "active":
                continue
            if fact.get("category") not in chosen_categories:
                continue
            if fact.get("polarity") not in chosen_polarities:
                continue
            scope = fact.get("scope_key") or {}
            if destination_key and fact.get("scope_type") in {"destination", "destination_companion"}:
                if _clean_text(scope.get("destination")) != destination_key:
                    continue
            if companion_key and fact.get("scope_type") in {"companion", "destination_companion"}:
                if companion_key not in _clean_text(scope.get("companion")):
                    continue
            item = {
                "id": fact.get("id"), "category": fact.get("category"),
                "value_text": fact.get("value_text"), "polarity": fact.get("polarity"),
                "scope_type": fact.get("scope_type"), "scope_key": scope,
            }
            if include_provenance:
                item["provenance"] = {
                    "source_kind": fact.get("source_kind"),
                    "source_conversation_id": fact.get("source_conversation_id"),
                    "evidence_sequences": list(fact.get("evidence_sequences") or [])[:20],
                }
            facts.append(item)
        facts.sort(key=lambda item: (str(item["category"]), str(item["id"])))
        bounded = facts[: max(1, min(int(limit), 50))]
        return {
            "memory_revision": context.memory_revision,
            "facts": bounded,
            "count": len(bounded),
            "has_more": len(facts) > len(bounded),
        }

    def get_planning_context(self, context: MainAgentContext) -> dict[str, Any]:
        brief = self.bridge.active_brief(context.user_id, context.conversation_id)
        explicit_itinerary = None
        if context.related_itinerary_id:
            explicit_itinerary = self.bridge.itinerary_summary(
                context.user_id, context.related_itinerary_id
            )
        return {
            "today": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "timezone": "Asia/Shanghai",
            "planning_brief": self.bridge.brief_context(brief),
            "missing_fields": list((brief or {}).get("missing_fields") or []),
            "active_runs": self.bridge.active_runs(context.user_id, context.conversation_id),
            "available_targets": self.bridge.conversation_targets(
                context.user_id, context.conversation_id
            ),
            "explicit_target": {
                "run_id": context.related_run_id,
                "itinerary_id": context.related_itinerary_id,
                "itinerary": explicit_itinerary,
            },
        }

    def find_saved_itineraries(
        self, context: MainAgentContext, *, destination: str | None = None,
        duration_days: int | None = None, travel_start_from: str | None = None,
        travel_start_to: str | None = None, include_revisions: bool = False,
        limit: int = 5,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        bounded_limit = max(1, min(int(limit), 5))
        decoded = self.bridge.list_itineraries(context.user_id)
        exact = [item for item in decoded if self._matches_itinerary(
            item, destination=destination, duration_days=duration_days,
            travel_start_from=travel_start_from, travel_start_to=travel_start_to)]
        near = [item for item in decoded if item not in exact and self._near_itinerary(
            item, destination=destination, duration_days=duration_days)]
        chosen = (exact or near)[:bounded_limit]
        match_kind = "exact" if exact else "near"
        artifact = None
        if chosen:
            artifact = {
                "kind": "itinerary_collection",
                "title": (f"{destination}保存的方案" if destination else "保存的旅行方案"),
                "match_kind": match_kind,
                "items": [self._model_itinerary(item) for item in chosen],
            }
        return ({
            "exact_matches": [self._model_itinerary(item) for item in exact[:bounded_limit]],
            "near_matches": [self._model_itinerary(item) for item in near[:bounded_limit]],
            "displayed_match_kind": match_kind if chosen else None,
            "count": len(chosen),
        }, artifact)

    def get_saved_itinerary(
        self, context: MainAgentContext, itinerary_id: str, day: int | None = None
    ) -> dict[str, Any]:
        item = self.bridge.itinerary_detail(context.user_id, itinerary_id)
        if not item:
            raise ValueError("itinerary not found")
        summary = self._model_itinerary(item)
        if day is None:
            return {"summary": summary, "highlights": item.get("highlights") or []}
        if int(day) < 1:
            raise ValueError("day must be at least 1")
        days = item.get("plan", {}).get("days") or []
        selected = next(
            (entry for index, entry in enumerate(days, 1)
             if int(entry.get("index") or entry.get("day") or index) == int(day)),
            None,
        )
        if selected is None:
            raise ValueError("requested day not found")
        return {"summary": summary, "day": {
            "index": selected.get("index") or selected.get("day"),
            "date": selected.get("date"),
            "theme": selected.get("theme"),
            "spots": [
                {"name": spot.get("name"), "arrive": spot.get("arrive"),
                 "leave": spot.get("leave"), "category": spot.get("category")}
                for spot in (selected.get("spots") or selected.get("timeline") or [])
            ],
        }}

    # ── 写入工具 ────────────────────────────────────
    async def update_current_brief(
        self, context: MainAgentContext, patch: dict[str, Any]
    ) -> AgentToolResult:
        brief, error = await self.bridge.apply_brief_patch(
            context.user_id, context.conversation_id, context.chat_run_id, patch
        )
        return AgentToolResult(
            ok=brief is not None,
            code="ok" if brief else "brief_validation_failed",
            message=error or "旅行需求已更新。",
            data={"brief": self.bridge.brief_context(brief)} if brief else {},
        )

    async def submit_current_brief(self, context: MainAgentContext) -> AgentToolResult:
        brief = self.bridge.active_brief(context.user_id, context.conversation_id)
        if not brief:
            brief = self.bridge.latest_brief(context.user_id, context.conversation_id)
        existing = self.bridge.run_result_itinerary(context.chat_run_id)
        if brief and brief.get("status") == "submitted" and existing:
            return AgentToolResult(
                ok=True, message="完整行程已经生成。",
                data={"brief_id": brief["id"], "itinerary_id": existing},
            )
        missing = required_brief_fields((brief or {}).get("data") or {})
        if missing:
            answer = interrupt(required_input_interrupt(missing))
            return AgentToolResult(
                ok=False, code="required_input_received",
                message="用户补充了必要信息。请解析后更新当前 Brief，并再次提交；如果仍缺字段，继续收集。",
                data={"answer": answer, "previous_missing_fields": missing},
            )
        if not brief:
            return AgentToolResult(ok=False, code="brief_not_ready",
                                   message="当前还没有可提交的旅行需求。")
        itinerary_id = await self.bridge.run_planner(
            context.user_id, context.conversation_id, context.chat_run_id, brief["id"]
        )
        if not itinerary_id:
            return AgentToolResult(ok=False, code="planning_failed",
                                   message="这次没能生成行程，请调整需求后重试。")
        return AgentToolResult(
            ok=True, message="完整行程已经生成。",
            data={"brief_id": brief["id"], "itinerary_id": itinerary_id},
        )

    async def start_revision(
        self, context: MainAgentContext, itinerary_id: str, modification_notes: str
    ) -> AgentToolResult:
        notes = modification_notes
        for _attempt in range(3):
            result_id, concern = await self.bridge.run_revision(
                context.user_id, context.conversation_id, context.chat_run_id,
                itinerary_id, notes,
            )
            if result_id:
                return AgentToolResult(
                    ok=True, message="行程修改已经完成。",
                    data={"itinerary_id": result_id},
                )
            if not concern:
                break
            answer = interrupt({
                "question": str(concern.get("question") or "请补充这次修改的必要信息"),
                "missing_fields": list(concern.get("missing_fields") or ["modification_notes"]),
                "input_schema": concern.get("input_schema") or {"type": "string", "minLength": 1},
            })
            notes = f"{notes}\n【用户补充】{str(answer).strip()}"
        return AgentToolResult(ok=False, code="revision_rejected",
                               message="修改信息仍不完整，请重新说明。")

    async def control_run(
        self, context: MainAgentContext, run_id: str, action: str
    ) -> AgentToolResult:
        controlled, error = await self.bridge.control_run(
            context.user_id, context.conversation_id, context.chat_run_id, run_id, action
        )
        return AgentToolResult(
            ok=controlled is not None,
            code="ok" if controlled else "run_control_rejected",
            message=error or "任务状态已更新。",
            data={"run": controlled} if controlled else {},
        )

    # ── 匹配辅助 ────────────────────────────────────
    @staticmethod
    def _matches_itinerary(
        item: dict[str, Any], *, destination: str | None, duration_days: int | None,
        travel_start_from: str | None, travel_start_to: str | None,
    ) -> bool:
        if destination and _clean_text(item["destination"]) != _clean_text(destination):
            return False
        if duration_days is not None and item["duration_days"] != duration_days:
            return False
        if travel_start_from and str(item.get("start_date") or "") < travel_start_from:
            return False
        if travel_start_to and str(item.get("start_date") or "") > travel_start_to:
            return False
        return True

    @staticmethod
    def _near_itinerary(
        item: dict[str, Any], *, destination: str | None, duration_days: int | None
    ) -> bool:
        destination_match = not destination or _clean_text(destination) in _clean_text(item["destination"])
        duration_match = duration_days is None or (
            item["duration_days"] is not None and abs(item["duration_days"] - duration_days) <= 1
        )
        return destination_match and duration_match

    @staticmethod
    def _model_itinerary(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item.get(key) for key in (
            "itinerary_id", "root_id", "version", "destination",
            "duration_days", "start_date", "end_date", "is_modified", "highlights",
        )}
