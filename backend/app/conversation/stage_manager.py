from typing import Any


STAGES = {
    "INTRO",
    "DISCOVERY",
    "RECOMMENDATION",
    "OBJECTION_HANDLING",
    "SITE_VISIT_BOOKING",
    "CLOSING",
}


def determine_stage(text: str, memory: dict[str, Any], objections: list[str] | None = None) -> str:
    return determine_stage_with_reason(text, memory, objections)["stage"]


def determine_stage_with_reason(text: str, memory: dict[str, Any], objections: list[str] | None = None) -> dict[str, Any]:
    lowered = str(text or "").lower()
    current = memory.get("conversation_stage") or "INTRO"
    objections = objections or []

    if len(lowered.strip()) < 15:
        stage = current if current in STAGES else "INTRO"
        return {"stage": stage, "reason": "short_transcript_keep_current_stage", "confidence": 0.0}
    if any(phrase in lowered for phrase in ("bye", "thank you", "not interested", "no requirement")):
        return {"stage": "CLOSING", "reason": "closing_intent_detected", "confidence": 0.9}
    if any(phrase in lowered for phrase in ("site visit", "visit", "kal aa", "aaj aa", "dekhne aa", "location bhej")):
        return {"stage": "SITE_VISIT_BOOKING", "reason": "visit_intent_detected", "confidence": 0.9}
    if objections:
        return {"stage": "OBJECTION_HANDLING", "reason": "objection_detected", "confidence": 0.85}
    if any(phrase in lowered for phrase in ("which project", "best project", "compare", "better", "suggest", "recommend")):
        return {"stage": "RECOMMENDATION", "reason": "recommendation_request", "confidence": 0.8}
    if any(phrase in lowered for phrase in ("price", "budget", "bhk", "bedroom", "location", "possession", "ready")):
        return {"stage": "DISCOVERY", "reason": "requirement_detail_detected", "confidence": 0.75}
    if current == "INTRO":
        return {"stage": "DISCOVERY", "reason": "first_meaningful_user_turn", "confidence": 0.55}
    stage = current if current in STAGES else "DISCOVERY"
    return {"stage": stage, "reason": "keep_current_stage", "confidence": 0.5}


def get_stage_context(memory: dict[str, Any]) -> str:
    stage = memory.get("conversation_stage") or "INTRO"
    return (
        "Conversation Stage:\n"
        f"- Current stage: {stage}\n"
        "- Stage guides the response but does not override the caller's latest intent.\n"
        "- Site visit booking has priority when caller shows readiness."
    )
