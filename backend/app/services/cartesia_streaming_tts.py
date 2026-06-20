import asyncio
import base64
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets

from app.config import Settings
from app.utils.audio import Pcm16ToUlaw8kTranscoder
from app.utils.logging import log


class CartesiaStreamingTTS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ws = None
        self._connect_lock = asyncio.Lock()
        self._cancelled_contexts: set[str] = set()

    async def connect(self) -> None:
        if self.ws:
            return
        if not self.settings.cartesia_api_key:
            raise RuntimeError("CARTESIA_API_KEY is not configured")
        query = urlencode({"cartesia_version": self.settings.cartesia_version})
        url = f"wss://api.cartesia.ai/tts/websocket?{query}"
        async with self._connect_lock:
            if self.ws:
                return
            log.info("cartesia_ws_connect_started", model_id=self.settings.cartesia_model_id)
            self.ws = await websockets.connect(
                url,
                additional_headers={"X-API-Key": self.settings.cartesia_api_key},
                ping_interval=10,
                ping_timeout=5,
                max_size=2**22,
            )
            log.info("cartesia_ws_connected", model_id=self.settings.cartesia_model_id)

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def cancel(self, context_id: str | None = None) -> None:
        if context_id:
            self._cancelled_contexts.add(context_id)
        if not self.ws:
            return
        if context_id:
            with contextlib.suppress(Exception):
                await self.ws.send(json.dumps({"context_id": context_id, "cancel": True}))
            return
        with contextlib.suppress(Exception):
            await self.ws.close()
        self.ws = None

    async def stream_ulaw_payloads(self, text: str, *, call_sid: str | None = None) -> AsyncIterator[tuple[str, str]]:
        await self.connect()
        context_id = str(uuid.uuid4())
        output_format = _cartesia_output_format(self.settings.cartesia_encoding, self.settings.cartesia_sample_rate)
        request = {
            "context_id": context_id,
            "model_id": self.settings.cartesia_model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self.settings.cartesia_voice_id},
            "language": self.settings.cartesia_language,
            "output_format": output_format,
            "add_timestamps": False,
            "continue": False,
        }
        log.info(
            "cartesia_stream_started",
            call_sid=call_sid,
            context_id=context_id,
            chars=len(text),
            model_id=self.settings.cartesia_model_id,
            encoding=self.settings.cartesia_encoding,
            sample_rate=self.settings.cartesia_sample_rate,
        )
        assert self.ws is not None
        await self.ws.send(json.dumps(request))
        transcoder = Pcm16ToUlaw8kTranscoder(input_sample_rate=self.settings.cartesia_sample_rate) if self.settings.cartesia_encoding != "pcm_mulaw" else None
        first_audio = False
        async for raw in self.ws:
            if context_id in self._cancelled_contexts:
                log.info("cartesia_stream_cancelled", context_id=context_id)
                return
            event = _parse_event(raw)
            if not event:
                continue
            event_context_id = str(event.get("context_id") or event.get("contextId") or context_id)
            if event_context_id != context_id:
                continue
            if event.get("type") == "error":
                log.error(
                    "cartesia_stream_error",
                    context_id=context_id,
                    status_code=event.get("status_code"),
                    error_code=event.get("error_code"),
                    message=event.get("message"),
                )
                raise RuntimeError(str(event.get("message") or "Cartesia stream error"))
            if event.get("type") in {"done", "complete", "completed"} or event.get("done"):
                break
            audio_b64 = event.get("data") or event.get("audio") or event.get("chunk")
            if not audio_b64:
                continue
            audio = base64.b64decode(str(audio_b64))
            payloads = [str(audio_b64)] if transcoder is None else transcoder.transcode_chunk_to_base64_frames(audio)
            for payload in payloads:
                if not first_audio:
                    first_audio = True
                    log.info("cartesia_first_audio", context_id=context_id, bytes=len(audio))
                yield context_id, payload
        if transcoder:
            for payload in transcoder.flush_base64_frames():
                yield context_id, payload
        log.info("cartesia_stream_finished", context_id=context_id)
        self._cancelled_contexts.discard(context_id)


def _cartesia_output_format(encoding: str, sample_rate: int) -> dict:
    if encoding == "pcm_mulaw":
        return {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": sample_rate}
    return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": sample_rate}


def _parse_event(raw) -> dict | None:
    if isinstance(raw, bytes):
        return {"data": base64.b64encode(raw).decode("ascii")}
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None
