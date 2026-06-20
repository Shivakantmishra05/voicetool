from collections.abc import AsyncIterator


SENTENCE_ENDINGS = (".", "?", "!", "।")
NATURAL_SHORT_REACTIONS = {
    "achha.",
    "haan ji.",
    "hmm.",
    "samajh gayi.",
    "theek hai.",
    "bilkul.",
}


async def sentence_fragments(
    deltas: AsyncIterator[str],
    *,
    min_chars: int,
    max_chars: int,
) -> AsyncIterator[str]:
    buffer = ""
    async for delta in deltas:
        buffer += delta
        while True:
            fragment, rest = _split_ready_fragment(buffer, min_chars=min_chars, max_chars=max_chars)
            if not fragment:
                break
            buffer = rest
            yield fragment
    tail = " ".join(buffer.split())
    if tail:
        yield tail


def _split_ready_fragment(text: str, *, min_chars: int, max_chars: int) -> tuple[str | None, str]:
    normalized = " ".join(text.split())
    if not normalized:
        return None, ""

    lowered = normalized.lower()
    if lowered in NATURAL_SHORT_REACTIONS:
        return normalized, ""

    for index, char in enumerate(normalized):
        if char in SENTENCE_ENDINGS:
            candidate = normalized[: index + 1].strip()
            if len(candidate) >= min_chars or candidate.lower() in NATURAL_SHORT_REACTIONS:
                return candidate, normalized[index + 1 :].strip()

    if len(normalized) >= max_chars:
        split_at = normalized.rfind(" ", 0, max_chars)
        if split_at < min_chars:
            split_at = max_chars
        return normalized[:split_at].strip(), normalized[split_at:].strip()

    return None, normalized
