import asyncio
import json
from time import monotonic
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app.utils.logging import log

TranscriptCallback = Callable[[str, bool, float | None], Awaitable[None]]
SpeechCallback = Callable[[], Awaitable[None]]


class DeepgramSTTStream:
    def __init__(self, settings: Settings, on_transcript: TranscriptCallback, on_speech_started: SpeechCallback):
        self.settings = settings
        self.on_transcript = on_transcript
        self.on_speech_started = on_speech_started
        self.ws = None
        self.listen_task = None
        self.keepalive_task = None
        self._closed = False
        self._last_send = monotonic()
        self._last_reconnect_attempt = 0.0
        self._reconnect_failures = 0
        self._reconnect_lock = asyncio.Lock()

    async def connect(self) -> None:
        current = asyncio.current_task()
        if self.listen_task and not self.listen_task.done() and self.listen_task is not current:
            self.listen_task.cancel()
        if self.keepalive_task and not self.keepalive_task.done() and self.keepalive_task is not current:
            self.keepalive_task.cancel()
        query = urlencode(
            {
                "model": self.settings.deepgram_model,
                "language": self.settings.deepgram_language,
                "encoding": "mulaw",
                "sample_rate": "8000",
                "channels": "1",
                "interim_results": "true",
                "smart_format": "true",
                "endpointing": str(self.settings.deepgram_endpointing_ms),
                "utterance_end_ms": str(self.settings.deepgram_utterance_end_ms),
                "vad_events": "true",
            }
        )
        url = f"wss://api.deepgram.com/v1/listen?{query}"
        safe_headers = {"Authorization": "Token ***"}
        log.info(
            "deepgram_connect_attempt",
            provider="deepgram",
            websocket_url=url,
            headers=safe_headers,
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            model=self.settings.deepgram_model,
            language=self.settings.deepgram_language,
        )
        try:
            self.ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {self.settings.deepgram_api_key}"},
                ping_interval=10,
                ping_timeout=5,
                max_size=2**22,
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            body = getattr(response, "body", None)
            log.exception(
                "deepgram_connect_failed",
                provider="deepgram",
                websocket_url=url,
                headers=safe_headers,
                status_code=getattr(response, "status_code", None),
                reason=getattr(response, "reason_phrase", None),
                dg_request_id=headers.get("dg-request-id") if headers else None,
                dg_error=headers.get("dg-error") if headers else None,
                response_body=_decode_response_body(body),
                error=str(exc),
            )
            raise
        self._closed = False
        self._reconnect_failures = 0
        self.listen_task = asyncio.create_task(self._listen())
        self.keepalive_task = asyncio.create_task(self._keepalive())
        log.info("deepgram_connected", model=self.settings.deepgram_model, endpointing_ms=self.settings.deepgram_endpointing_ms)

    async def send_audio(self, audio: bytes) -> None:
        for attempt in range(self.settings.deepgram_reconnect_attempts + 1):
            try:
                if not self.ws:
                    await self.connect()
                await self.ws.send(audio)
                self._last_send = monotonic()
                return
            except Exception as exc:
                log.warning("deepgram_send_failed", attempt=attempt, error=str(exc))
                await self._reconnect()
        log.error("deepgram_audio_frame_dropped", attempts=self.settings.deepgram_reconnect_attempts + 1)

    async def close(self) -> None:
        self._closed = True
        if self.keepalive_task:
            self.keepalive_task.cancel()
        if self.ws:
            await self.ws.close()
        if self.listen_task:
            try:
                await asyncio.wait_for(self.listen_task, timeout=self.settings.provider_cleanup_timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.listen_task.cancel()

    async def _listen(self) -> None:
        try:
            async for message in self.ws:
                await self._handle_raw_message(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            log.warning(
                "deepgram_stream_closed",
                error=str(exc),
                deepgram_close_code=exc.code,
                deepgram_close_reason=exc.reason,
            )
            if not self._closed:
                await self._reconnect()
        except Exception as exc:
            log.warning("deepgram_stream_closed", error=str(exc), deepgram_close_code=_close_code(self.ws))
            if not self._closed:
                await self._reconnect()
        else:
            if not self._closed:
                log.warning("deepgram_stream_ended", deepgram_close_code=_close_code(self.ws))
                await self._reconnect()

    async def _handle_raw_message(self, message) -> None:
        message_type = type(message).__name__
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            log.warning("deepgram_invalid_json", deepgram_raw_message_type=message_type, error=str(exc))
            return

        payload_type = type(payload).__name__
        if isinstance(payload, list):
            log.debug("deepgram_raw_message_type", deepgram_raw_message_type=payload_type, item_count=len(payload))
            for item in payload:
                if isinstance(item, dict):
                    await self._handle_payload(item)
                else:
                    log.debug("deepgram_raw_event", event_type=None, payload_type=type(item).__name__, ignored=True)
            return

        if isinstance(payload, dict):
            log.debug("deepgram_raw_message_type", deepgram_raw_message_type=payload_type)
            await self._handle_payload(payload)
            return

        log.debug("deepgram_raw_event", event_type=None, payload_type=payload_type, ignored=True)

    async def _handle_payload(self, payload: dict) -> None:
        event_type = payload.get("type")
        log.debug("deepgram_raw_event", event_type=event_type, payload_type="dict")
        if event_type == "SpeechStarted":
            await self.on_speech_started()
            return
        if event_type not in {"Results", "UtteranceEnd"}:
            return

        channel = payload.get("channel")
        if not isinstance(channel, dict):
            log.debug("deepgram_raw_event", event_type=event_type, payload_type=type(channel).__name__, ignored=True)
            return

        alternatives = channel.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            return

        first = alternatives[0]
        if not isinstance(first, dict):
            log.debug("deepgram_raw_event", event_type=event_type, payload_type=type(first).__name__, ignored=True)
            return

        transcript = (first.get("transcript") or "").strip()
        if transcript:
            await self.on_transcript(
                transcript,
                bool(payload.get("is_final") or payload.get("speech_final")),
                first.get("confidence"),
            )

    async def _keepalive(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.settings.deepgram_keepalive_seconds)
            if not self.ws:
                continue
            if monotonic() - self._last_send >= self.settings.deepgram_keepalive_seconds:
                try:
                    await self.ws.send(json.dumps({"type": "KeepAlive"}))
                    self._last_send = monotonic()
                except Exception as exc:
                    log.warning("deepgram_keepalive_failed", error=str(exc))
                    await self._reconnect()

    async def _reconnect(self) -> None:
        if self._closed:
            return
        async with self._reconnect_lock:
            if self._closed:
                return
            now = monotonic()
            wait_seconds = max(0.0, 1.0 - (now - self._last_reconnect_attempt))
            if wait_seconds:
                log.warning("deepgram_reconnect_throttled", wait_seconds=round(wait_seconds, 3))
                await asyncio.sleep(wait_seconds)
            self._last_reconnect_attempt = monotonic()
            try:
                if self.ws:
                    await self.ws.close()
            except Exception:
                pass
            self.ws = None
            await asyncio.sleep(min(2.0, 0.15 * (self._reconnect_failures + 1)))
            try:
                await self.connect()
                self._reconnect_failures = 0
                log.info("deepgram_reconnected")
            except Exception as exc:
                self._reconnect_failures += 1
                log.warning("deepgram_reconnect_failed", error=str(exc))


def _decode_response_body(body) -> str | None:
    if body is None:
        return None
    try:
        return bytes(body).decode("utf-8", errors="replace")[:1000]
    except Exception:
        return repr(body)[:1000]


def _close_code(ws) -> int | None:
    return getattr(ws, "close_code", None) if ws else None
