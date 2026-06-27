from typing import Any


SUPPORTED_LANGUAGES = {"hinglish", "hindi", "english"}
NEGATION_WORDS = ("mat", "nahi", "nahin", "don't", "dont", "not", "avoid")


def _is_negated(lowered: str, keyword: str) -> bool:
    idx = lowered.find(keyword)
    if idx == -1:
        return False
    preceding = lowered[max(0, idx - 24) : idx]
    following = lowered[idx : idx + 32]
    return any(neg in preceding.split() for neg in NEGATION_WORDS) or any(
        neg in following.split() for neg in NEGATION_WORDS
    )


def detect_language_update(text: str, memory: dict[str, Any]) -> dict[str, Any]:
    """
    Detects if user wants to change language.
    Returns update dict — empty dict means no change needed.

    FIX: Explicit language requests ALWAYS override the current lock,
    even if a language was locked earlier. This allows customers to
    switch Hindi -> English -> Hindi freely during the call.
    """
    lowered = str(text or "").lower()

    # English request. Do not hard-lock forever; the next explicit language
    # request should override immediately.
    if "english" in lowered and _is_negated(lowered, "english"):
        return {"language": "hinglish", "language_locked": False}

    if any(phrase in lowered for phrase in (
        "english please", "speak english", "in english",
        "english me", "english mein", "english bol",
        "let's speak english", "talk in english",
        "english mein bolo", "english mein bole",
    )):
        return {"language": "english", "language_locked": False}

    # Hindi request
    if any(phrase in lowered for phrase in (
        "hindi me", "hindi mein", "continue in hindi",
        "speak hindi", "hindi please", "hindi bol",
        "hindi mein baat", "hindi me baat",
        "hindi mein bolo", "hindi mein bole",
        "हिंदी", "हिन्दी", "हिंदी में", "हिन्दी में",
        "हिंदी में बात", "हिन्दी में बात",
    )):
        return {"language": "hindi", "language_locked": False}

    # Hinglish request
    if any(phrase in lowered for phrase in (
        "hinglish", "mix language", "hindi english mix",
        "hinglish mein", "hinglish me",
    )):
        return {"language": "hinglish", "language_locked": False}

    # No explicit request — keep current language, no change.
    # NOTE: Previously this returned early when language_locked was True,
    # which permanently blocked switching back. That early-return is
    # intentionally removed so the explicit checks above always apply.
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

    if language == "english":
        instruction = (
            "Current language: English.\n"
            "- Respond ONLY in English. No Hindi or Hinglish words at all.\n"
            "- Do NOT use 'Haan', 'Ji', 'Sir', 'Achha', 'Theek hai' or any Hindi filler.\n"
            "- Sound like a warm, confident Indian woman speaking fluent English.\n"
            "- Use reactions only when natural; do not start every response with one.\n"
            "- Keep same personality — warm, direct, consultative.\n"
            "- IMPORTANT: If the caller switches back to Hindi/Hinglish, follow them immediately "
            "in your NEXT response — do not stay stuck in English."
        )
    elif language == "hindi":
        instruction = (
            "Current language: Hindi.\n"
            "- Bol poori tarah Hindi mein. Koi English word mat use karo.\n"
            "- Fillers optional hain; har reply 'Haan ji' ya 'Samajh gayi' se start mat karo.\n"
            "- Feminine grammar zaroori: 'bol rahi hoon', 'samajh gayi'.\n"
            "- IMPORTANT: Agar caller English mein switch kare, turant English mein "
            "respond karo apne next turn mein — Hindi mein atke mat raho."
        )
    elif language == "hinglish":
        instruction = (
            "Language: Hinglish (default).\n"
            "- Natural mix of Hindi and English — jaise ek real Indian consultant bolta/bolti hai phone pe.\n"
            "- Fillers optional hain; same filler repeat mat karo, aur har response filler se start mat karo.\n"
            "- Feminine grammar: 'bol rahi hoon', 'samajh gayi'.\n"
            "- Do NOT switch to full English just because caller mentions English words, car, Java, code, or an unrelated topic.\n"
            "- Off-topic redirect bhi Hinglish mein hi do. Example: 'Samajh gayi sir, lekin main property side hi guide kar paungi.'"
        )
    else:
        instruction = f"Language: {language}. Follow naturally."

    return (
        "Language State:\n"
        f"- Current language: {language}\n"
        f"- Language locked: {locked}\n"
        f"- {instruction}\n"
        "- If the caller explicitly asks to switch language (e.g. 'Hindi mein bolo', "
        "'speak English'), switch IMMEDIATELY in your next response, regardless of any "
        "previous lock.\n"
        "- If caller does NOT explicitly ask to switch language, never change language for refusals or redirects."
    )
