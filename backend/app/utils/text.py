def sanitize_tts(text: str) -> str:
    text = text.replace("*", "")
    text = text.replace("#", "")
    text = text.replace("\n", " ")
    text = " ".join(text.split())

    words = text.split()

    if len(words) > 35:
        text = " ".join(words[:35])

    return text.strip()
