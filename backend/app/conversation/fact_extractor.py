import re
from typing import Any


FACT_FIELDS = (
    "customer_name",
    "budget",
    "bhk",
    "property_type",
    "location_interest",
    "project_preference",
    "purpose",
    "self_use_or_investment",
    "visit_interest",
    "whatsapp_consent",
    "timeline",
    "decision_maker",
)


def extract_customer_facts(text: str, current_memory: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned = _normalize(text)
    if not cleaned:
        return {}

    updates: dict[str, Any] = {}
    _extract_name(cleaned, updates)
    _extract_budget(cleaned, updates)
    _extract_bhk(cleaned, updates)
    _extract_location(cleaned, updates)
    _extract_purpose(cleaned, updates)
    _extract_visit_interest(cleaned, updates)
    _extract_whatsapp_consent(cleaned, updates)
    _extract_timeline(cleaned, updates)
    _extract_decision_maker(cleaned, updates)
    _extract_refusals(cleaned, updates)

    if not current_memory:
        return updates
    return {field: value for field, value in updates.items() if _should_update(field, value, current_memory)}


def _extract_name(text: str, updates: dict[str, Any]) -> None:
    patterns = (
        r"\b(?:mera naam|my name is|name is)\s+([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:hai|he|is)\b|$)",
        r"\b(?:naam)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+(?:hai|he)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = _title(match.group(1))
            if name and name.lower() not in {"budget", "investment", "self use"}:
                updates["customer_name"] = name
                return


def _extract_budget(text: str, updates: dict[str, Any]) -> None:
    if _mentions_refusal(text, ("budget",)):
        updates["refused_fields"] = ["budget"]
        return
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(cr|crore|crores|lakh|lakhs|lac|lacs|k|thousand)?\b", text, re.IGNORECASE)
    if not match:
        return
    nearby = text[max(0, match.start() - 12) : match.end() + 12].lower()
    unit = (match.group(2) or "").lower()
    if any(token in nearby for token in ("bhk", "bedroom", "bed room", "sector")):
        return
    if not unit and not any(token in text.lower() for token in ("budget", "price", "range", "tak", "around")):
        return
    unit = unit or "lakh"
    value = match.group(1)
    if unit in {"cr", "crore", "crores"}:
        updates["budget"] = f"{value} crore"
    elif unit in {"k", "thousand"}:
        updates["budget"] = f"{value} thousand"
    else:
        updates["budget"] = f"{value} lakh"


def _extract_bhk(text: str, updates: dict[str, Any]) -> None:
    if re.search(r"\b(?:1|one)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "1 BHK"
        updates["property_type"] = "1 BHK"
    elif re.search(r"\b(?:2|two)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "2 BHK"
        updates["property_type"] = "2 BHK"
    elif re.search(r"\b(?:3|three)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "3 BHK"
        updates["property_type"] = "3 BHK"
    elif re.search(r"\b(?:4|four)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "4 BHK"
        updates["property_type"] = "4 BHK"


def _extract_location(text: str, updates: dict[str, Any]) -> None:
    locations = {
        "sector 150": "Sector 150 Noida",
        "greater noida west": "Greater Noida West",
        "greater noida": "Greater Noida",
        "noida": "Noida",
        "green valley": "Green Valley",
        "skyline heights": "Skyline Heights",
        "skyline": "Skyline Heights",
    }
    lowered = text.lower()
    for key, value in locations.items():
        if key in lowered:
            updates["location_interest"] = value
            if value in {"Green Valley", "Skyline Heights"}:
                updates["project_preference"] = value
            return


def _extract_purpose(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(word in lowered for word in ("investment", "invest", "rental income")):
        updates["purpose"] = "investment"
        updates["self_use_or_investment"] = "investment"
    elif any(word in lowered for word in ("self use", "self-use", "khud", "family", "rehne")):
        updates["purpose"] = "self-use"
        updates["self_use_or_investment"] = "self-use"


def _extract_visit_interest(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("site visit", "visit", "dekhne", "kab aa", "aa sakta")):
        if any(neg in lowered for neg in ("nahi", "not", "no ")):
            updates["visit_interest"] = "not interested yet"
        else:
            updates["visit_interest"] = "interested"


def _extract_whatsapp_consent(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("whatsapp kar", "whatsapp pe", "bhej do", "send kar", "details bhej", "brochure bhej")):
        if any(neg in lowered for neg in ("nahi", "not", "no ")):
            updates["whatsapp_consent"] = "declined"
        else:
            updates["whatsapp_consent"] = "yes"


def _extract_timeline(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    timeline_phrases = (
        "ready to move",
        "ready-to-move",
        "immediate",
        "jaldi",
        "this month",
        "next month",
        "december 2026",
        "within 3 months",
        "within 6 months",
    )
    for phrase in timeline_phrases:
        if phrase in lowered:
            updates["timeline"] = phrase.replace("-", " ")
            return


def _extract_decision_maker(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("main decide", "i decide", "mera decision", "i am decision maker")):
        updates["decision_maker"] = "self"
    elif any(phrase in lowered for phrase in ("family se", "wife se", "husband se", "parents se", "partner se")):
        updates["decision_maker"] = "needs approval"


def _extract_refusals(text: str, updates: dict[str, Any]) -> None:
    refused = list(updates.get("refused_fields") or [])
    if _mentions_refusal(text, ("budget",)):
        refused.append("budget")
    if _mentions_refusal(text, ("name", "naam")):
        refused.append("customer_name")
    if refused:
        updates["refused_fields"] = sorted(set(refused))


def _mentions_refusal(text: str, fields: tuple[str, ...]) -> bool:
    lowered = text.lower()
    if not any(field in lowered for field in fields):
        return False
    return any(phrase in lowered for phrase in ("nahi batana", "not tell", "don't want", "mat pucho", "skip"))


def _should_update(field: str, value: Any, memory: dict[str, Any]) -> bool:
    if field == "refused_fields":
        return True
    current = memory.get(field)
    if not current:
        return True
    return len(str(value)) > len(str(current)) and str(value).lower() != str(current).lower()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _title(text: str) -> str:
    return " ".join(part.capitalize() for part in _normalize(text).split())
