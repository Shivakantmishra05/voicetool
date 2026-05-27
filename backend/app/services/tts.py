import base64
import asyncio

import httpx

from app.config import Settings
from app.utils.audio import Pcm16ToUlaw8kTranscoder
from app.utils.logging import log


class ElevenLabsTTS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.tts_timeout_seconds))

    @property
    def output_format(self) -> str:
        return self.settings.elevenlabs_output_format

    async def close(self) -> None:
        await self.client.aclose()

    async def synthesize_ulaw_chunks(self, text: str):
        last_error: Exception | None = None
        voice_ids = _voice_candidates(self.settings.elevenlabs_voice_id, self.settings.elevenlabs_fallback_voice_id)
        for voice_index, voice_id in enumerate(voice_ids):
            should_retry_voice = voice_index == 0 and len(voice_ids) > 1
            for attempt in range(2):
                try:
                    async for payload in self._stream_voice(text, voice_id, attempt):
                        yield payload
                    return
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    detail = _safe_response_text(exc.response)
                    log.warning(
                        "elevenlabs_tts_http_error",
                        status_code=exc.response.status_code,
                        output_format_used=self.settings.elevenlabs_output_format,
                        voice_id_suffix=voice_id[-6:],
                        using_fallback_voice=voice_index > 0,
                        response_body=detail,
                    )
                    if exc.response.status_code == 402 and should_retry_voice:
                        log.warning("elevenlabs_voice_restricted_using_fallback", primary_voice_suffix=voice_id[-6:])
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.2)
                    else:
                        break
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "elevenlabs_tts_error",
                        error=str(exc),
                        output_format_used=self.settings.elevenlabs_output_format,
                        voice_id_suffix=voice_id[-6:],
                        using_fallback_voice=voice_index > 0,
                    )
                    if attempt == 0:
                        await asyncio.sleep(0.2)
                    else:
                        break
        raise last_error or RuntimeError("ElevenLabs TTS failed")

    async def _stream_voice(self, text: str, voice_id: str, attempt: int):
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format={self.settings.elevenlabs_output_format}"
        log.info(
            "elevenlabs_tts_request",
            output_format_used=self.settings.elevenlabs_output_format,
            model_id=self.settings.elevenlabs_model_id,
            voice_id_suffix=voice_id[-6:],
            attempt=attempt,
            chars=len(text),
        )
        transcoder = Pcm16ToUlaw8kTranscoder() if self.settings.elevenlabs_output_format == "pcm_16000" else None
        async with self.client.stream(
            "POST",
            url,
            headers={"xi-api-key": self.settings.elevenlabs_api_key, "content-type": "application/json"},
            json={
                "text": text,
                "model_id": self.settings.elevenlabs_model_id,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
            },
        ) as stream:
            stream.raise_for_status()
            async for chunk in stream.aiter_bytes():
                if not chunk:
                    continue
                if transcoder:
                    for payload in transcoder.transcode_chunk_to_base64_frames(chunk):
                        yield payload
                else:
                    yield base64.b64encode(chunk).decode("ascii")
            if transcoder:
                for payload in transcoder.flush_base64_frames():
                    yield payload

    async def fallback_ulaw_chunks(self):
        # 160 bytes of 8 kHz mu-law silence. Twilio can play it safely if TTS is unavailable.
        silence = base64.b64encode(b"\xff" * 160).decode("ascii")
        yield silence


def _voice_candidates(primary: str, fallback: str | None) -> list[str]:
    voices = [primary]
    if fallback and fallback != primary:
        voices.append(fallback)
    return voices


def _safe_response_text(response: httpx.Response) -> str:
    try:
        return response.text[:1000]
    except Exception:
        return ""
