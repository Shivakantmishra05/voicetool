from typing import Any

LINKED_FIELDS = {
    "property_type": "bhk",
    "bhk": "property_type",
}


def can_ask(memory: dict[str, Any], field: str) -> bool:
    refused = set(memory.get("refused_fields") or [])
    if field in refused:
        return False
    if memory.get(field):
        return False
    linked = LINKED_FIELDS.get(field)
    if linked and memory.get(linked):
        return False
    asked = memory.get("asked_questions") or {}
    if isinstance(asked, dict):
        return int(asked.get(field) or 0) < 2
    return list(asked).count(field) < 2


def record_question(memory: dict[str, Any], field: str) -> dict[str, Any]:
    asked = memory.get("asked_questions") or {}
    if isinstance(asked, dict):
        updated = dict(asked)
        updated[field] = int(updated.get(field) or 0) + 1
        return {"asked_questions": updated}
    counts: dict[str, int] = {}
    for item in asked:
        counts[item] = counts.get(item, 0) + 1
    counts[field] = counts.get(field, 0) + 1
    return {"asked_questions": counts}


def infer_asked_field(text: str) -> str | None:
    lowered = str(text or "").lower()
    if any(word in lowered for word in ("budget", "kitna", "price range")):
        return "budget"
    if any(word in lowered for word in ("location", "kis side", "noida", "greater noida")):
        return "location_interest"
    if any(word in lowered for word in ("2 bhk", "3 bhk", "bedroom", "flat dekh")):
        return "bhk"
    if any(word in lowered for word in ("investment", "self-use", "self use")):
        return "purpose"
    if any(word in lowered for word in ("visit", "site")):
        return "visit_interest"
    if any(word in lowered for word in ("timeline", "kab tak", "possession", "ready")):
        return "timeline"
    return None
