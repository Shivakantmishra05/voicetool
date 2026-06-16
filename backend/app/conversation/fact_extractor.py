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
    "current_city",
    "visit_day",
    "callback_time",
)

AGENT_NAMES = {"riya", "riya sharma", "dreamhome", "dreamhome properties"}
BUDGET_CONTEXT_WORDS = (
    "budget", "price", "range", "cost", "rate", "under", "around",
    "tak", "mein", "ke andar", "lakh", "lac", "crore", "cr"
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
    _extract_current_city(cleaned, updates)
    _extract_visit_day(cleaned, updates)
    _extract_callback_time(cleaned, updates)
    _extract_refusals(cleaned, updates)

    if not current_memory:
        return updates
    return {field: value for field, value in updates.items() if _should_update(field, value, current_memory)}


def _extract_name(text: str, updates: dict[str, Any]) -> None:
    patterns = (
        r"\b(?:mera naam|my name is|name is|mai|main)\s+([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:hai|he|is|hoon|hun)\b|$)",
        r"\b(?:naam)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+(?:hai|he)\b",
        r"\b([A-Z][a-z]{2,15})\s+(?:bol raha|bol rahi|speaking|here)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = _title(match.group(1))
            normalized_name = name.lower()
            if name and normalized_name not in AGENT_NAMES and normalized_name not in {
                "budget", "investment", "self use", "noida", "delhi",
                "property", "flat", "bhk", "bedroom",
            }:
                updates["customer_name"] = name
                return


def _extract_budget(text: str, updates: dict[str, Any]) -> None:
    if _mentions_refusal(text, ("budget",)):
        updates["refused_fields"] = ["budget"]
        return

    # Match patterns like "50 lakh", "1.5 crore", "45L", "50k"
    match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(cr|crore|crores|lakh|lakhs|lac|lacs|l\b|k|thousand)?\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return

    nearby = text[max(0, match.start() - 20) : match.end() + 20].lower()
    unit = (match.group(2) or "").lower().rstrip(".")

    # Skip if number is near BHK or sector references
    if any(token in nearby for token in ("bhk", "bedroom", "bed room", "sector", "floor")):
        return

    # Require a money unit or clear nearby budget context. This prevents
    # "Sector 150" / "3 BHK" type numbers from becoming budgets.
    if not unit and not any(token in nearby for token in BUDGET_CONTEXT_WORDS):
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
    """Extract BHK preference — supports 1/2/3/4 BHK."""
    if re.search(r"\b(?:1|one|ek)\s*(?:bhk|bedroom|bed room|room)\b", text, re.IGNORECASE):
        updates["bhk"] = "1 BHK"
        updates["property_type"] = "1 BHK"
    elif re.search(r"\b(?:2|two|do)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "2 BHK"
        updates["property_type"] = "2 BHK"
    elif re.search(r"\b(?:3|three|teen)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "3 BHK"
        updates["property_type"] = "3 BHK"
    elif re.search(r"\b(?:4|four|char)\s*(?:bhk|bedroom|bed room)\b", text, re.IGNORECASE):
        updates["bhk"] = "4 BHK"
        updates["property_type"] = "4 BHK"


def _extract_location(text: str, updates: dict[str, Any]) -> None:
    """Extract preferred project location or project name."""
    locations = {
        "sector 150": "Sector 150 Noida",
        "sector 10": "The Greens",
        "sector 4": "Lotus Residency",
        "sector 1": "Orchid Heights",
        "greater noida west": "Greater Noida West",
        "greater noida": "Greater Noida",
        "noida expressway": "Noida Expressway",
        "noida": "Noida",
        "green valley": "Green Valley",
        "orchid heights": "Orchid Heights",
        "orchid": "Orchid Heights",
        "lotus residency": "Lotus Residency",
        "lotus": "Lotus Residency",
        "the greens": "The Greens",
        "skyline heights": "Skyline Heights",
        "skyline": "Skyline Heights",
    }
    lowered = text.lower()
    for key, value in locations.items():
        if key in lowered:
            updates["location_interest"] = value
            if value in {"Green Valley", "Orchid Heights", "Lotus Residency", "The Greens", "Skyline Heights"}:
                updates["project_preference"] = value
            return


def _extract_current_city(text: str, updates: dict[str, Any]) -> None:
    """Extract where caller currently lives."""
    patterns = [
        r"abhi\s+(?:main\s+|hum\s+)?([a-zA-Z\u0900-\u097F ]{3,20}?)\s+(?:mein|me|side|area|pe)\s+(?:rehta|rehti|hu|hoon|hai)",
        r"([a-zA-Z\u0900-\u097F]{3,15})\s+(?:side|area)\s+(?:mein\s+)?(?:rehta|rehti|hu|hoon)",
        r"(?:from|se)\s+([a-zA-Z]{4,15})\s+(?:hu|hoon|hai|bol)",
        r"([a-zA-Z]{4,15})\s+(?:wala|wali)\s+hu",
    ]
    skip_words = {
        "abhi", "yahan", "wahan", "ghar", "flat", "property",
        "noida", "delhi", "greater", "budget", "price",
    }
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            city = match.group(1).strip().title()
            if city.lower() not in skip_words and len(city) > 2:
                updates["current_city"] = city
                return


def _extract_purpose(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(word in lowered for word in ("investment", "invest", "rental income", "kiraye", "rent out")):
        updates["purpose"] = "investment"
        updates["self_use_or_investment"] = "investment"
    elif any(word in lowered for word in ("self use", "self-use", "khud", "family", "rehne", "apne liye", "ghar chahiye")):
        updates["purpose"] = "self-use"
        updates["self_use_or_investment"] = "self-use"


def _extract_visit_interest(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    negative = any(neg in lowered for neg in ("nahi", "nahin", "not", "no ", "abhi nahi", "baad mein"))
    if any(phrase in lowered for phrase in ("site visit", "visit", "dekhne", "kab aa", "aa sakta", "dikhaiye")):
        updates["visit_interest"] = "not interested yet" if negative else "interested"


def _extract_whatsapp_consent(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in (
        "whatsapp kar", "whatsapp pe", "bhej do", "send kar",
        "details bhej", "brochure bhej", "wa pe", "wp pe",
    )):
        negative = any(neg in lowered for neg in ("nahi", "not", "no "))
        updates["whatsapp_consent"] = "declined" if negative else "yes"


def _extract_timeline(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    timeline_map = {
        "ready to move": "ready to move",
        "ready-to-move": "ready to move",
        "immediate": "immediate",
        "jaldi": "immediate",
        "abhi chahiye": "immediate",
        "this month": "this month",
        "next month": "next month",
        "is saal": "this year",
        "within 3 months": "within 3 months",
        "within 6 months": "within 6 months",
        "december 2026": "December 2026",
        "june 2026": "June 2026",
        "march 2027": "March 2027",
        "2 saal": "2 years",
        "ek saal": "1 year",
    }
    for phrase, value in timeline_map.items():
        if phrase in lowered:
            updates["timeline"] = value
            return


def _extract_visit_day(text: str, updates: dict[str, Any]) -> None:
    """Extract preferred site visit day."""
    day_map = {
        "sunday": "Sunday",
        "sanday": "Sunday",
        "saturday": "Saturday",
        "monday": "Monday",
        "tuesday": "Tuesday",
        "wednesday": "Wednesday",
        "thursday": "Thursday",
        "friday": "Friday",
        "weekend": "Weekend",
        "kal": "tomorrow",
        "aaj": "today",
        "parso": "day after tomorrow",
        "is hafte": "this week",
        "agle hafte": "next week",
    }
    lowered = text.lower()
    for key, val in day_map.items():
        if key in lowered:
            updates["visit_day"] = val
            return


def _extract_callback_time(text: str, updates: dict[str, Any]) -> None:
    """Extract callback time when client says they are busy."""
    lowered = text.lower()

    busy_signals = ("busy", "abhi nahi", "baad mein", "bad mein", "call back", "callback", "later", "free nahi")
    if not any(phrase in lowered for phrase in busy_signals):
        # Also extract if they give a time without busy context (scheduling)
        time_match = re.search(r"\b(\d{1,2})\s*(?:baje|am|pm|o'clock)\b", text, re.IGNORECASE)
        day_match = re.search(
            r"\b(kal|sunday|saturday|monday|tuesday|wednesday|thursday|friday|evening|subah|shaam|dopahar)\b",
            text,
            re.IGNORECASE,
        )
        if time_match or day_match:
            parts = []
            if day_match:
                parts.append(day_match.group(1).capitalize())
            if time_match:
                parts.append(f"{time_match.group(1)} baje")
            if parts:
                updates["callback_time"] = " ".join(parts)
        return

    # Busy — extract when they want callback
    time_match = re.search(r"\b(\d{1,2})\s*(?:baje|am|pm|o'clock)\b", text, re.IGNORECASE)
    day_match = re.search(
        r"\b(kal|sunday|saturday|monday|tuesday|wednesday|thursday|friday|evening|subah|shaam|dopahar|shyam)\b",
        text,
        re.IGNORECASE,
    )
    parts = []
    if day_match:
        parts.append(day_match.group(1).capitalize())
    if time_match:
        parts.append(f"{time_match.group(1)} baje")
    updates["callback_requested"] = True
    if parts:
        updates["callback_time"] = " ".join(parts)


def _extract_decision_maker(text: str, updates: dict[str, Any]) -> None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("main decide", "i decide", "mera decision", "i am decision maker", "main hi dunga")):
        updates["decision_maker"] = "self"
    elif any(phrase in lowered for phrase in ("family se", "wife se", "husband se", "parents se", "partner se", "discuss karna")):
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
    return any(phrase in lowered for phrase in ("nahi batana", "not tell", "don't want", "mat pucho", "skip", "rehne do"))


def _should_update(field: str, value: Any, memory: dict[str, Any]) -> bool:
    if field in {"refused_fields", "callback_requested"}:
        return True
    current = memory.get(field)
    if not current:
        return True
    # Don't downgrade specificity
    return len(str(value)) >= len(str(current)) and str(value).lower() != str(current).lower()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _title(text: str) -> str:
    return " ".join(part.capitalize() for part in _normalize(text).split())
