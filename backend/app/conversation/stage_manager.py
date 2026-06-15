"""
stage_manager.py — Conversation stage detection.

Key fix from v1:
- "abhi nahi" no longer jumps to CLOSING (it's an objection, not a goodbye)
- Stage can't regress from SITE_VISIT_BOOKING or CLOSING to earlier stages
- Short transcripts don't force stage change
"""

from typing import Any


STAGES = {
    "INTRO",
    "DISCOVERY",
    "RECOMMENDATION",
    "OBJECTION_HANDLING",
    "SITE_VISIT_BOOKING",
    "CLOSING",
}

# Stages that should never regress to an earlier stage
TERMINAL_STAGES = {"SITE_VISIT_BOOKING", "CLOSING"}

# Stage ordering for regression guard
STAGE_ORDER = {
    "INTRO": 0,
    "DISCOVERY": 1,
    "RECOMMENDATION": 2,
    "OBJECTION_HANDLING": 2,  # parallel to recommendation
    "SITE_VISIT_BOOKING": 3,
    "CLOSING": 4,
}


def determine_stage(text: str, memory: dict[str, Any], objections: list[str] | None = None) -> str:
    return determine_stage_with_reason(text, memory, objections)["stage"]


def determine_stage_with_reason(
    text: str, memory: dict[str, Any], objections: list[str] | None = None
) -> dict[str, Any]:
    lowered = str(text or "").lower()
    current = memory.get("conversation_stage") or "INTRO"
    objections = objections or []

    # Too short to make a decision — keep current
    if len(lowered.strip()) < 15:
        stage = current if current in STAGES else "INTRO"
        return {"stage": stage, "reason": "short_transcript_keep_current", "confidence": 0.0}

    # ── Hard exits — genuine goodbye signals only ────────────────
    # "abhi nahi" is NOT a goodbye — it's an objection. Only explicit goodbye phrases trigger CLOSING.
    genuine_goodbye = (
        "no requirement", "not interested", "do not call",
        "mat call karo", "remove my number", "band karo",
        "khatam karo", "bye", "goodbye", "thank you bye",
    )
    if any(phrase in lowered for phrase in genuine_goodbye):
        # Don't close on first "not interested" — let the agent handle it
        # Only close if memory says this has happened before
        not_interested_count = memory.get("not_interested_count") or 0
        if not_interested_count >= 1 or any(p in lowered for p in ("do not call", "mat call karo", "remove my number", "band karo")):
            return {"stage": "CLOSING", "reason": "explicit_exit_intent", "confidence": 0.95}

    # ── Visit booking signals ────────────────────────────────────
    visit_signals = (
        "site visit", "visit karte hain", "kal aa", "aaj aa",
        "dekhne aana", "site dekhna", "location bhej", "aa sakta hoon",
        "aa sakti hoon", "weekend aana", "sunday aana",
    )
    if any(phrase in lowered for phrase in visit_signals):
        return {"stage": "SITE_VISIT_BOOKING", "reason": "visit_intent", "confidence": 0.9}

    # ── Objection handling — "abhi nahi" lives here ─────────────
    if objections:
        return {"stage": "OBJECTION_HANDLING", "reason": f"objections: {objections}", "confidence": 0.85}

    # Explicit objection phrases that detector might miss
    soft_objection_phrases = (
        "abhi nahi", "sochna hai", "baad mein", "family se poochna",
        "wife se poochna", "discuss karna", "thoda time chahiye",
        "mehenga hai", "budget nahi", "compare karna",
    )
    if any(phrase in lowered for phrase in soft_objection_phrases):
        return {"stage": "OBJECTION_HANDLING", "reason": "soft_objection_detected", "confidence": 0.8}

    # ── Recommendation ───────────────────────────────────────────
    recommendation_phrases = (
        "which project", "best project", "compare", "suggest",
        "recommend", "kaunsa better", "kaun sa better", "which one",
        "green valley", "orchid", "lotus", "skyline", "the greens",
    )
    if any(phrase in lowered for phrase in recommendation_phrases):
        new_stage = "RECOMMENDATION"
        return {"stage": _guard_regression(current, new_stage), "reason": "recommendation_request", "confidence": 0.8}

    # ── Discovery ────────────────────────────────────────────────
    discovery_phrases = (
        "price", "budget", "bhk", "bedroom", "location",
        "possession", "ready", "kitna", "kaun sa area", "noida",
        "greater noida", "investment", "self use", "khud ke liye",
    )
    if any(phrase in lowered for phrase in discovery_phrases):
        new_stage = "DISCOVERY"
        return {"stage": _guard_regression(current, new_stage), "reason": "requirement_detail", "confidence": 0.75}

    # ── Default: move from INTRO → DISCOVERY on any real turn ───
    if current == "INTRO":
        return {"stage": "DISCOVERY", "reason": "first_real_user_turn", "confidence": 0.55}

    # Keep current
    stage = current if current in STAGES else "DISCOVERY"
    return {"stage": stage, "reason": "keep_current", "confidence": 0.5}


def _guard_regression(current: str, proposed: str) -> str:
    """Never let stage go backwards. Terminal stages are permanent."""
    if current in TERMINAL_STAGES:
        return current
    current_order = STAGE_ORDER.get(current, 0)
    proposed_order = STAGE_ORDER.get(proposed, 0)
    if proposed_order < current_order:
        return current
    return proposed


def get_stage_context(memory: dict[str, Any]) -> str:
    stage = memory.get("conversation_stage") or "INTRO"
    hints = {
        "INTRO":              "Warm welcome. Confirm identity. Get permission to talk.",
        "DISCOVERY":          "Find out what they need — one question at a time. Don't rush.",
        "RECOMMENDATION":     "Suggest best-fit project with one clear reason. Then offer visit.",
        "OBJECTION_HANDLING": "Address the objection naturally. Don't argue. Offer a way forward.",
        "SITE_VISIT_BOOKING": "Lock in the visit day + WhatsApp. Then close warmly.",
        "CLOSING":            "Warm, graceful exit. Leave door open for future.",
    }.get(stage, "Follow the caller's lead.")

    return (
        f"Stage: {stage}\n"
        f"Focus: {hints}\n"
        "Note: Caller's immediate need overrides stage — if they want to visit, book it now."
    )