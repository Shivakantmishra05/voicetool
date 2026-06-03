from typing import Any


OBJECTIONS = {"PRICE", "LOCATION", "TIMING", "TRUST", "COMPARISON", "FAMILY_APPROVAL", "WAITING"}


def detect_objections(text: str) -> list[str]:
    lowered = str(text or "").lower()
    found: list[str] = []
    if any(phrase in lowered for phrase in ("too expensive", "mehenga", "mahanga", "budget high", "price high", " costly", "zyada rate")):
        found.append("PRICE")
    if any(phrase in lowered for phrase in ("location dur", "far", "door", "traffic", "area pasand nahi")):
        found.append("LOCATION")
    if any(phrase in lowered for phrase in ("late possession", "possession late", "time lagega", "abhi nahi")):
        found.append("TIMING")
    if any(phrase in lowered for phrase in ("trust", "rera", "builder kaun", "genuine", "fraud")):
        found.append("TRUST")
    if any(phrase in lowered for phrase in ("compare", "comparison", "dusra project", "aur project", "better option")):
        found.append("COMPARISON")
    if any(phrase in lowered for phrase in ("family se", "wife se", "husband se", "parents se", "discuss")):
        found.append("FAMILY_APPROVAL")
    if any(phrase in lowered for phrase in ("later", "baad me", "maybe later", "soch ke", "abhi nahi")):
        found.append("WAITING")
    return found


def merge_objections(memory: dict[str, Any], objections: list[str]) -> list[str]:
    return sorted(set(memory.get("objections") or []) | set(objections))


def get_objection_context(memory: dict[str, Any]) -> str:
    objections = memory.get("objections") or []
    if not objections:
        return "Objections:\n- None detected."
    return (
        "Objections:\n"
        f"- Detected: {', '.join(objections)}\n"
        "- Address only the latest relevant objection naturally. Do not argue."
    )

