from __future__ import annotations

import re
from typing import Any


PROJECTS = {
    "green_valley": {
        "name": "Green Valley",
        "location": "Greater Noida West",
        "reason": "ready to move hai aur metro paas hai",
        "prices": {"1 BHK": "28L", "2 BHK": "45L", "3 BHK": "68L"},
    },
    "orchid_heights": {
        "name": "Orchid Heights",
        "location": "Sector 1 Greater Noida West",
        "reason": "budget mein ready-to-move dekh sakte hain",
        "prices": {"1 BHK": "25L", "2 BHK": "42L", "3 BHK": "62L"},
    },
    "lotus_residency": {
        "name": "Lotus Residency",
        "location": "Sector 4 Greater Noida West",
        "reason": "balanced Greater Noida West option hai",
        "prices": {"2 BHK": "52L", "3 BHK": "78L"},
    },
    "the_greens": {
        "name": "The Greens",
        "location": "Sector 10 Greater Noida West",
        "reason": "us side log investment ke liye bhi dekh rahe hain",
        "prices": {"2 BHK": "48L", "3 BHK": "72L"},
    },
    "skyline_heights": {
        "name": "Skyline Heights",
        "location": "Sector 150 Noida",
        "reason": "Sector 150 Noida side dekh rahe hain to fit ho sakta hai",
        "prices": {"2 BHK": "65L", "3 BHK": "95L"},
    },
}


def match_project(memory: dict[str, Any]) -> dict[str, str] | None:
    text = _memory_text(memory)
    budget_lakh = _budget_lakh(memory.get("budget"))
    bhk = str(memory.get("bhk") or memory.get("property_type") or "").upper()

    explicit = _explicit_project(text)
    if explicit:
        return _result(explicit, bhk)

    if _contains(text, "ready", "ready to move", "immediate", "shift"):
        if budget_lakh is not None and budget_lakh <= 43:
            return _result("orchid_heights", bhk)
        return _result("green_valley", bhk)

    if _contains(text, "investment", "investor", "rental income", "rent out", "roi"):
        if _contains(text, "premium", "sector 150", "noida"):
            return _result("skyline_heights", bhk)
        return _result("the_greens", bhk)

    if budget_lakh is not None and budget_lakh <= 45:
        return _result("orchid_heights", bhk)

    if _contains(text, "premium", "sector 150", "noida"):
        return _result("skyline_heights", bhk)

    if _contains(text, "greater noida", "greater noida west"):
        return _result("green_valley", bhk)

    return None


def project_context(memory: dict[str, Any]) -> str:
    match = match_project(memory)
    confidence = recommendation_confidence(memory, match)
    has_minimum_context = _has_minimum_recommendation_context(memory)
    if not match or confidence < 70 or not has_minimum_context:
        return (
            "Recommendation confidence:\n"
            f"- Score: {confidence}\n"
            "- Do not recommend yet. Need location + purpose + BHK or budget first.\n"
            "- Ask one natural clarification only if it fits the caller's current intent."
        )
    return (
        "Matched project:\n"
        f"- Project: {match['project_name']}\n"
        f"- Reason: {match['reason']}\n"
        f"- Recommendation confidence: {confidence}/100\n"
        "- Action: Softly recommend one project.\n"
        "- Use only this project unless caller explicitly asks for comparison.\n"
        "- Wording must be soft: 'dekh sakte hain', 'ho sakta hai suit kare', never 'best/perfect'."
    )


def recommendation_confidence(memory: dict[str, Any], match: dict[str, str] | None = None) -> int:
    score = 0
    if match:
        score += 30
    if memory.get("location_interest") or memory.get("preferred_location"):
        score += 20
    if memory.get("bhk") or memory.get("property_type"):
        score += 20
    if memory.get("budget"):
        score += 15
    if memory.get("purpose") or memory.get("self_use_or_investment"):
        score += 10
    if memory.get("timeline") or memory.get("buying_timeline"):
        score += 5
    if not match:
        return min(score, 65)
    return min(score, 100)


def _has_minimum_recommendation_context(memory: dict[str, Any]) -> bool:
    has_location = bool(memory.get("location_interest") or memory.get("preferred_location"))
    has_purpose = bool(memory.get("purpose") or memory.get("self_use_or_investment"))
    has_size_or_budget = bool(memory.get("bhk") or memory.get("property_type") or memory.get("budget"))
    return has_location and has_purpose and has_size_or_budget


def _result(project_key: str, bhk: str) -> dict[str, str]:
    project = PROJECTS[project_key]
    reason = project["reason"]
    price = project["prices"].get(bhk)
    if price:
        reason = f"{reason}; {bhk} {price} se hai"
    return {"project_name": project["name"], "reason": reason}


def _explicit_project(text: str) -> str | None:
    if "green valley" in text:
        return "green_valley"
    if "orchid" in text:
        return "orchid_heights"
    if "lotus" in text:
        return "lotus_residency"
    if "the greens" in text or "greens" in text:
        return "the_greens"
    if "skyline" in text or "sector 150" in text:
        return "skyline_heights"
    return None


def _memory_text(memory: dict[str, Any]) -> str:
    fields = (
        "budget",
        "bhk",
        "property_type",
        "location_interest",
        "preferred_location",
        "project_preference",
        "purpose",
        "self_use_or_investment",
        "timeline",
        "buying_timeline",
        "customer_profile",
    )
    return " ".join(str(memory.get(field) or "") for field in fields).lower()


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _budget_lakh(value: Any) -> float | None:
    text = str(value or "").lower().replace(",", "")
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    amount = float(match.group(1))
    if any(unit in text for unit in ("cr", "crore")):
        return amount * 100
    return amount
