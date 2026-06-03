from typing import Any


PROFILES = {"UNKNOWN", "SELF_USE", "INVESTOR", "BROKER", "TENANT", "EXPLORING"}


def classify_customer_profile(text: str, memory: dict[str, Any]) -> str:
    return classify_customer_profile_with_confidence(text, memory)["profile"]


def classify_customer_profile_with_confidence(text: str, memory: dict[str, Any]) -> dict[str, Any]:
    lowered = str(text or "").lower()
    if len(lowered.strip()) < 15:
        return {"profile": "UNKNOWN", "confidence": 0.0, "reason": "short_transcript"}
    if any(word in lowered for word in ("roi", "rental yield", "investment", "invest", "appreciation")):
        return {"profile": "INVESTOR", "confidence": 0.9, "reason": "investment_keyword"}
    if any(word in lowered for word in ("family", "school", "hospital", "children", "kids", "khud", "self use", "rehne")):
        return {"profile": "SELF_USE", "confidence": 0.85, "reason": "self_use_keyword"}
    if any(word in lowered for word in ("broker", "client", "customer ke liye", "party ke liye")):
        return {"profile": "BROKER", "confidence": 0.9, "reason": "broker_keyword"}
    if any(word in lowered for word in ("rent", "lease", "rental")):
        return {"profile": "TENANT", "confidence": 0.85, "reason": "rental_keyword"}
    existing = memory.get("customer_profile")
    if existing and existing != "UNKNOWN":
        return {"profile": existing, "confidence": 0.55, "reason": "existing_memory"}
    return {"profile": "EXPLORING", "confidence": 0.35, "reason": "default_exploring"}


def get_profile_context(memory: dict[str, Any]) -> str:
    profile = memory.get("customer_profile") or "UNKNOWN"
    guidance = {
        "UNKNOWN": "Do light discovery only; do not assume intent yet.",
        "INVESTOR": "Focus on ROI, future appreciation, connectivity, and location growth.",
        "SELF_USE": "Focus on family comfort, connectivity, lifestyle, nearby schools and hospitals.",
        "BROKER": "Stay professional, ask for buyer requirement, avoid over-sharing unconfirmed details.",
        "TENANT": "Current inventory is sale-focused; redirect politely to purchase options.",
        "EXPLORING": "Keep discovery light and move toward WhatsApp details or visit.",
    }.get(profile, "Keep discovery light and move toward WhatsApp details or visit.")
    return f"Customer Profile:\n- Profile: {profile}\n- Guidance: {guidance}"
