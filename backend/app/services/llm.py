import asyncio
import json
from typing import Any

from google import genai
from google.genai import types

from app.config import Settings
from app.prompts.real_estate_agent import SUMMARY_PROMPT, SYSTEM_PROMPT
from app.services.retry import retry_async


class GeminiLLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)

    async def next_response(self, turns: list[dict[str, Any]]) -> str:
        recent = turns[-self.settings.max_turns_in_prompt :]
        contents = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in recent)

        async def run() -> str:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.settings.gemini_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.45,
                        max_output_tokens=90,
                    ),
                ),
                timeout=self.settings.llm_timeout_seconds,
            )
            text = (response.text or "").strip()
            return self._clean_phone_text(text)

        return await retry_async(run, attempts=2)

    async def stream_response(self, turns: list[dict[str, Any]]):
        recent = turns[-self.settings.max_turns_in_prompt :]
        contents = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in recent)
        yielded = False
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.45,
                    max_output_tokens=90,
                ),
            )
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                total = ""
                async for chunk in stream:
                    text = getattr(chunk, "text", None)
                    if not text:
                        continue
                    total += text
                    if len(total) > self.settings.agent_response_max_chars:
                        break
                    yielded = True
                    yield text
        except Exception:
            if yielded:
                return
            text = await self.next_response(turns)
            yield text

    async def summarize(self, turns: list[dict[str, Any]]) -> dict[str, Any]:
        transcript = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in turns)

        async def run() -> dict[str, Any]:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.settings.gemini_model,
                    contents=transcript,
                    config=types.GenerateContentConfig(
                        system_instruction=SUMMARY_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                ),
                timeout=8,
            )
            return json.loads(response.text or "{}")

        return await retry_async(run, attempts=2)

    def fallback_response(self) -> str:
        return "Ji sir, DreamHome Properties me 2 BHK aur 3 BHK options available hain. Aapka approximate budget kya rahega?"

    def _clean_phone_text(self, text: str) -> str:
        text = " ".join(text.replace("\n", " ").split())
        if len(text) <= self.settings.agent_response_max_chars:
            return text
        return text[: self.settings.agent_response_max_chars].rsplit(" ", 1)[0] + "."
