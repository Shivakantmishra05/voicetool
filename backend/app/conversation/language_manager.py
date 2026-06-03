from typing import Any


SUPPORTED_LANGUAGES = {"hinglish", "hindi", "english"}


def detect_language_update(text: str, memory: dict[str, Any]) -> dict[str, Any]:
    lowered = str(text or "").lower()
    if any(phrase in lowered for phrase in ("english please", "speak english", "in english", "english me", "english mein")):
        return {"language": "english", "language_locked": True}
    if any(phrase in lowered for phrase in ("hindi me", "hindi mein", "continue in hindi", "speak hindi", "hindi please")):
        return {"language": "hindi", "language_locked": True}
    if any(phrase in lowered for phrase in ("hinglish", "mix language", "hindi english")):
        return {"language": "hinglish", "language_locked": True}
    if memory.get("language_locked"):
        return {}
    return {"language": memory.get("language") or "hinglish"}


def get_language_context(memory: dict[str, Any]) -> str:
    language = memory.get("language") or "hinglish"
    locked = bool(memory.get("language_locked"))
    if language not in SUPPORTED_LANGUAGES:
        language = "hinglish"
    return (
        "Language State:\n"
        f"- Current language: {language}\n"
        f"- Language locked: {locked}\n"
        "- Follow this language state exactly. Do not randomly switch language."
    )

