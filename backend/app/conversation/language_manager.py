from typing import Any


SUPPORTED_LANGUAGES = {"hinglish", "hindi", "english"}


def detect_language_update(text: str, memory: dict[str, Any]) -> dict[str, Any]:
    """
    Detects if user wants to change language.
    Returns update dict — empty dict means no change needed.

    FIX: Previously language lock was set but persona fillers were still
    in Hinglish. Now the language field is properly propagated so
    get_persona_context() can switch fillers accordingly.
    """
    lowered = str(text or "").lower()

    # English request
    if any(phrase in lowered for phrase in (
        "english please", "speak english", "in english",
        "english me", "english mein", "english bol",
        "let's speak english", "talk in english",
    )):
        return {"language": "english", "language_locked": True}

    # Hindi request
    if any(phrase in lowered for phrase in (
        "hindi me", "hindi mein", "continue in hindi",
        "speak hindi", "hindi please", "hindi bol",
        "hindi mein baat", "hindi me baat",
    )):
        return {"language": "hindi", "language_locked": True}

    # Hinglish request
    if any(phrase in lowered for phrase in (
        "hinglish", "mix language", "hindi english mix",
    )):
        return {"language": "hinglish", "language_locked": True}

    # Language already locked — don't override
    if memory.get("language_locked"):
        return {}

    # No change
    return {}


def get_language_context(memory: dict[str, Any]) -> str:
    """
    Returns language instruction for the LLM system prompt.

    FIX: Now gives explicit, strict instruction about what language
    to use — including telling AI NOT to mix in Hindi fillers when
    English is locked.
    """
    language = memory.get("language") or "hinglish"
    locked = bool(memory.get("language_locked"))

    if language not in SUPPORTED_LANGUAGES:
        language = "hinglish"

    if language == "english" and locked:
        instruction = (
            "STRICT ENGLISH MODE (locked by user):\n"
            "- Respond ONLY in English. No Hindi or Hinglish words at all.\n"
            "- Do NOT use 'Haan', 'Ji', 'Sir', 'Achha', 'Theek hai' or any Hindi filler.\n"
            "- Sound like a warm, confident Indian woman speaking fluent English.\n"
            "- Use natural English reactions: 'Right.', 'Got it.', 'Makes sense.', 'Sure thing.'\n"
            "- Keep same personality — warm, direct, consultative."
        )
    elif language == "hindi" and locked:
        instruction = (
            "STRICT HINDI MODE (locked by user):\n"
            "- Bol poori tarah Hindi mein. Koi English word mat use karo.\n"
            "- Fillers: 'Haan ji', 'Achha', 'Samajh gayi', 'Bilkul'.\n"
            "- Feminine grammar zaroori: 'bol rahi hoon', 'samajh gayi'."
        )
    elif language == "hinglish":
        instruction = (
            "Language: Hinglish (default).\n"
            "- Natural mix of Hindi and English — jaise ek real Indian consultant bolta/bolti hai phone pe.\n"
            "- Fillers: 'Haan sir', 'Achha', 'Samajh gayi', 'Bilkul', 'Theek hai'.\n"
            "- Feminine grammar: 'bol rahi hoon', 'samajh gayi'."
        )
    else:
        instruction = f"Language: {language}. Follow naturally."

    return (
        "Language State:\n"
        f"- Current language: {language}\n"
        f"- Language locked: {locked}\n"
        f"- {instruction}"
    )