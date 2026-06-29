from typing import Any


def calculate_lead_score(memory: dict[str, Any]) -> int:
    score = 0
    if memory.get("budget"):
        score += 20
    if memory.get("bhk") or memory.get("property_type"):
        score += 15
    if memory.get("location_interest") or memory.get("preferred_location"):
        score += 15
    if memory.get("purpose") or memory.get("self_use_or_investment"):
        score += 15
    if _positive_visit(memory.get("visit_interest")):
        score += 40
    if _within_30_days(memory.get("timeline") or memory.get("buying_timeline")):
        score += 50
    if memory.get("decision_maker"):
        score += 25
    return min(score, 100)


def get_lead_score_context(memory: dict[str, Any]) -> str:
    score = int(memory.get("lead_score") or 0)
    if score >= 70:
        status = "hot"
    elif score >= 40:
        status = "warm"
    else:
        status = "early"
    return (
        f"Lead Score:\n- Score: {score}\n- Priority: {status}\n"
        "- If visit intent is present, confirm one simple next step; do not reopen discovery."
    )


def _positive_visit(value: Any) -> bool:
    return str(value or "").lower() in {"interested", "yes", "confirmed", "site visit", "visit"}


def _within_30_days(value: Any) -> bool:
    lowered = str(value or "").lower()
    return any(phrase in lowered for phrase in ("immediate", "jaldi", "this month", "within 30", "next month"))
