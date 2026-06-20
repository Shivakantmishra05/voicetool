import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings
from app.utils.logging import log


class OpenAITextLLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.openai_text_timeout_seconds))

    async def close(self) -> None:
        await self.client.aclose()

    async def stream_response(self, *, instructions: str, turns: list[dict[str, Any]]) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": instructions}]
        for turn in turns[-self.settings.max_turns_in_prompt :]:
            role = _role(turn)
            text = str(turn.get("text") or "").strip()
            if text:
                messages.append({"role": role, "content": text})

        log.info(
            "openai_text_stream_started",
            model=self.settings.openai_text_model,
            turns=len(messages) - 1,
            max_tokens=self.settings.openai_text_max_tokens,
        )
        first_delta = False
        async with self.client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.openai_text_model,
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": self.settings.openai_text_max_tokens,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("openai_text_stream_bad_json", data_preview=data[:120])
                    continue
                delta = (((event.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
                if not delta:
                    continue
                if not first_delta:
                    first_delta = True
                    log.info("openai_text_first_delta", model=self.settings.openai_text_model)
                yield delta
        log.info("openai_text_stream_completed", model=self.settings.openai_text_model)


def _role(turn: dict[str, Any]) -> str:
    role = str(turn.get("role") or turn.get("speaker") or "user").lower()
    if role == "assistant":
        return "assistant"
    return "user"
