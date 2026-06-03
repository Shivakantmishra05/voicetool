import json
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis


DEFAULT_MEMORY: dict[str, Any] = {
    # Identity
    "customer_name": None,
    "current_city": None,
    # Language
    "language": "hinglish",
    "language_locked": False,
    # Call type
    "call_type": "outgoing",  # "outgoing" or "incoming"
    # Intent & profile
    "intent_type": None,
    "customer_profile": "UNKNOWN",
    # Property requirement
    "budget": None,
    "bhk": None,
    "property_type": None,
    "location_interest": None,
    "preferred_location": None,
    "project_preference": None,
    "purpose": None,
    "self_use_or_investment": None,
    "timeline": None,
    "buying_timeline": None,
    # Visit & follow-up
    "visit_interest": None,
    "visit_day": None,
    "visit_time": None,
    "whatsapp_consent": None,
    # Callback (for busy clients)
    "callback_requested": False,
    "callback_time": None,
    # Conversation state
    "conversation_stage": "INTRO",
    "objections": [],
    "lead_score": 0,
    "decision_maker": None,
    # Anti-repetition
    "asked_questions": [],
    "refused_fields": [],
    # Metrics
    "conversation_metrics": {},
    "facts_updated_at": None,
}


class CallMemoryManager:
    def __init__(self, redis_client: Redis | None = None, ttl_seconds: int = 86400):
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self._local: dict[str, dict[str, Any]] = {}

    async def load_memory(self, call_sid: str) -> dict[str, Any]:
        if self.redis:
            raw = await self.redis.get(self._key(call_sid))
            if raw:
                return _merge_defaults(json.loads(raw))
        return _merge_defaults(self._local.get(call_sid) or {})

    async def save_memory(self, call_sid: str, memory: dict[str, Any]) -> dict[str, Any]:
        normalized = _merge_defaults(memory)
        if self.redis:
            await self.redis.set(self._key(call_sid), json.dumps(normalized), ex=self.ttl_seconds)
        self._local[call_sid] = normalized
        return normalized

    async def update_memory(self, call_sid: str, updates: dict[str, Any]) -> dict[str, Any]:
        memory = await self.load_memory(call_sid)
        changed = False

        for field, value in updates.items():
            if field == "refused_fields":
                existing = set(memory.get("refused_fields") or [])
                merged = sorted(existing | set(value or []))
                if merged != memory.get("refused_fields"):
                    memory["refused_fields"] = merged
                    changed = True
                continue

            if field == "objections":
                existing = set(memory.get("objections") or [])
                merged = sorted(existing | set(value or []))
                if merged != memory.get("objections"):
                    memory["objections"] = merged
                    changed = True
                continue

            if field == "asked_questions":
                existing = memory.get("asked_questions") or []
                if isinstance(existing, dict):
                    merged = dict(existing)
                    question_counts = (value or {}).items() if isinstance(value, dict) else []
                    for question, count in question_counts:
                        new_count = max(int(count or 1), int(merged.get(question) or 0))
                        if new_count != merged.get(question):
                            merged[question] = new_count
                            changed = True
                    memory["asked_questions"] = merged
                else:
                    merged_list = list(existing)
                    for question in value or []:
                        if question not in merged_list:
                            merged_list.append(question)
                            changed = True
                    memory["asked_questions"] = merged_list
                continue

            if field == "conversation_metrics":
                merged = dict(memory.get("conversation_metrics") or {})
                merged.update(value or {})
                if merged != memory.get("conversation_metrics"):
                    memory["conversation_metrics"] = merged
                    changed = True
                continue

            if field == "language_locked" and value != memory.get(field):
                memory[field] = bool(value)
                changed = True
                continue

            if field == "callback_requested" and value != memory.get(field):
                memory[field] = bool(value)
                changed = True
                continue

            if field == "language" and value in {"hinglish", "hindi", "english"} and value != memory.get(field):
                memory[field] = value
                changed = True
                continue

            if field == "lead_score" and value != memory.get(field):
                memory[field] = int(value or 0)
                changed = True
                continue

            if field in DEFAULT_MEMORY and value is not None and _is_more_specific(value, memory.get(field)):
                memory[field] = value
                changed = True

        if changed:
            memory["facts_updated_at"] = datetime.now(timezone.utc).isoformat()

        return await self.save_memory(call_sid, memory)

    @staticmethod
    def _key(call_sid: str) -> str:
        return f"call:{call_sid}:conversation_memory"


def build_memory_context(memory: dict[str, Any]) -> str:
    facts = []
    labels = {
        "customer_name": "Customer name",
        "current_city": "Currently lives in",
        "language": "Language",
        "call_type": "Call type",
        "intent_type": "Intent",
        "budget": "Budget",
        "bhk": "BHK preference",
        "property_type": "Property type",
        "location_interest": "Location interest",
        "project_preference": "Project preference",
        "purpose": "Purpose",
        "self_use_or_investment": "Self-use or investment",
        "visit_interest": "Visit interest",
        "visit_day": "Preferred visit day",
        "whatsapp_consent": "WhatsApp consent",
        "callback_time": "Callback time",
        "timeline": "Possession timeline",
        "conversation_stage": "Stage",
        "customer_profile": "Profile",
        "lead_score": "Lead score",
    }

    for field, label in labels.items():
        val = memory.get(field)
        if val:
            facts.append(f"- {label}: {val}")

    asked = memory.get("asked_questions") or []
    refused = memory.get("refused_fields") or []

    lines = ["Current Call Memory:"]
    lines.extend(facts or ["- No confirmed facts yet."])

    if asked:
        if isinstance(asked, dict):
            asked_text = ", ".join(f"{f} ({c}x)" for f, c in asked.items())
        else:
            asked_text = ", ".join(asked)
        lines.append("- Already asked: " + asked_text)

    if refused:
        lines.append("- Caller refused: " + ", ".join(refused))

    objections = memory.get("objections") or []
    if objections:
        lines.append("- Objections: " + ", ".join(objections))

    if memory.get("callback_requested"):
        cb_time = memory.get("callback_time") or "time not specified"
        lines.append(f"- Callback requested at: {cb_time}. Do not push further.")

    if facts or asked or refused:
        lines.append("- NEVER ask again for known or refused fields.")

    return "\n".join(lines)


async def load_memory(call_sid: str, manager: CallMemoryManager) -> dict[str, Any]:
    return await manager.load_memory(call_sid)


async def save_memory(call_sid: str, memory: dict[str, Any], manager: CallMemoryManager) -> dict[str, Any]:
    return await manager.save_memory(call_sid, memory)


async def update_memory(call_sid: str, updates: dict[str, Any], manager: CallMemoryManager) -> dict[str, Any]:
    return await manager.update_memory(call_sid, updates)


def _merge_defaults(memory: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_MEMORY)
    merged.update(memory or {})
    asked = merged.get("asked_questions") or []
    merged["asked_questions"] = dict(asked) if isinstance(asked, dict) else list(asked)
    merged["refused_fields"] = list(merged.get("refused_fields") or [])
    merged["objections"] = list(merged.get("objections") or [])
    return merged


def _is_more_specific(new_value: Any, current_value: Any) -> bool:
    if current_value in (None, "", [], False):
        return True
    if str(new_value).lower() == str(current_value).lower():
        return False
    if current_value in {"INTRO", "DISCOVERY", "RECOMMENDATION", "OBJECTION_HANDLING", "SITE_VISIT_BOOKING", "CLOSING"}:
        return True
    if str(current_value).upper() in {"UNKNOWN", "SELF_USE", "INVESTOR", "BROKER", "TENANT", "EXPLORING"}:
        return str(new_value).upper() not in {"UNKNOWN", "EXPLORING"} or str(current_value).upper() == "UNKNOWN"
    return len(str(new_value)) >= len(str(current_value))
