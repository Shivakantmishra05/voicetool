import asyncio
import base64
import contextlib
import json
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app.database.session import SessionLocal
from app.models import Call
from app.prompts.real_estate_agent import SYSTEM_PROMPT
from app.observability import Metrics
from app.services.call_repository import CallRepository
from app.services.call_summary import CallSummarizer
from app.services.memory import ConversationMemory, ConversationState
from app.services.supabase_crm import SupabaseCRM
from app.telephony.stream_auth import StreamClaims, StreamTokenService
from app.utils.logging import call_sid_ctx, log


OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
GREETING = "Hello, Udaan Residency PG Indirapuram se bol raha hu. Aap boys PG dekh rahe hain ya girls PG?"


class TwilioMediaSession:
    def __init__(
        self,
        websocket: WebSocket,
        settings: Settings,
        memory: ConversationMemory,
        stream_tokens: StreamTokenService,
        claims: StreamClaims,
        metrics: Metrics,
        summarizer: CallSummarizer,
        crm: SupabaseCRM,
    ):
        self.websocket = websocket
        self.settings = settings
        self.memory = memory
        self.stream_tokens = stream_tokens
        self.claims = claims
        self.metrics = metrics
        self.summarizer = summarizer
        self.crm = crm

        self.state: ConversationState | None = None
        self.call: Call | None = None
        self.openai_ws = None
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.stopping = False
        self.transcript_turns: list[dict[str, str]] = []
        self.assistant_transcript_parts: dict[str, list[str]] = {}

    async def run(self) -> None:
        await self.websocket.accept()
        self.metrics.inc("voice_ws_connected_total")
        try:
            await self._twilio_loop()
        except (WebSocketDisconnect, asyncio.TimeoutError):
            await self._stop(failure_reason="websocket disconnected or idle timeout")
        except Exception as exc:
            log.exception("twilio_realtime_session_failed", error=str(exc))
            await self._stop(failure_reason=str(exc))
        finally:
            await self._cancel_tasks()
            await self._close_openai()
            self.metrics.inc("voice_ws_disconnected_total")

    async def _twilio_loop(self) -> None:
        while not self.stopping:
            raw = await asyncio.wait_for(self.websocket.receive_text(), timeout=self.settings.call_idle_timeout_seconds)
            if len(raw) > self.settings.websocket_max_message_chars:
                raise ValueError("websocket message too large")
            await self._handle_twilio_message(json.loads(raw))

    async def _handle_twilio_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "start":
            await self._start(message)
        elif event == "media":
            await self._media(message)
        elif event == "mark":
            log.debug("twilio_mark_ack", mark_name=(message.get("mark") or {}).get("name"))
        elif event == "stop":
            await self._stop()

    async def _start(self, message: dict) -> None:
        if self.started:
            raise ValueError("duplicate Twilio start frame")
        start = message.get("start", {})
        call_sid = start.get("callSid") or message.get("callSid")
        stream_sid = start.get("streamSid") or message.get("streamSid")
        caller = (start.get("customParameters") or {}).get("From")
        media_format = start.get("mediaFormat") or {}
        if not call_sid or not stream_sid:
            raise ValueError("Twilio start message missing callSid or streamSid")
        self._validate_twilio_audio_format(media_format)
        await self.stream_tokens.consume_for_start(self.claims, call_sid)
        self.started = True
        call_sid_ctx.set(call_sid)

        self.state = await self.memory.get(call_sid) or ConversationState(call_sid=call_sid)
        self.state.stream_sid = stream_sid
        self.state.caller_number = caller
        await self.memory.set(self.state)

        async with SessionLocal() as session:
            repo = CallRepository(session)
            self.call = await repo.start_call(call_sid, stream_sid, caller)

        log.info("call_started", stream_sid=stream_sid, media_format=media_format, provider="openai_realtime")
        await self._connect_openai()
        self._spawn(self._openai_loop(), "openai_realtime_receiver")

    async def _connect_openai(self) -> None:
        url = f"{OPENAI_REALTIME_URL}?model={self.settings.openai_realtime_model}"
        log.info("openai_realtime_connecting", model=self.settings.openai_realtime_model)
        self.openai_ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "OpenAI-Safety-Identifier": self.claims.call_sid,
            },
            ping_interval=10,
            ping_timeout=5,
            max_size=2**23,
        )
        log.info("openai_realtime_connected", model=self.settings.openai_realtime_model)
        await self._send_openai(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": _realtime_instructions(),
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            "transcription": {"model": self.settings.openai_transcription_model},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.55,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 650,
                                "interrupt_response": True,
                                "create_response": True,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": "marin",
                            "speed": 0.95,
                        },
                    },
                },
            }
        )
        log.info("openai_realtime_session_update_sent")
        await self._send_openai(
            {
                "type": "response.create",
                "response": {
                    "modalities": ["audio"],
                    "instructions": f"Start the call now by saying exactly: {GREETING}",
                },
            }
        )
        log.info("openai_realtime_greeting_requested")

    async def _media(self, message: dict) -> None:
        if not self.openai_ws:
            return
        payload = (message.get("media") or {}).get("payload")
        if not payload:
            return
        try:
            base64.b64decode(payload, validate=True)
        except Exception:
            self.metrics.inc("voice_bad_media_payload_total")
            return
        await self._send_openai({"type": "input_audio_buffer.append", "audio": payload})
        self.metrics.inc("voice_twilio_media_frames_total")

    async def _openai_loop(self) -> None:
        if not self.openai_ws:
            return
        try:
            async for raw in self.openai_ws:
                event = json.loads(raw)
                await self._handle_openai_event(event)
        except ConnectionClosed as exc:
            if not self.stopping:
                log.warning("openai_realtime_closed", code=exc.code, reason=exc.reason)
                await self._stop(failure_reason=f"openai realtime closed: {exc.code}")
        except Exception as exc:
            if not self.stopping:
                log.exception("openai_realtime_receiver_failed", error=str(exc))
                await self._stop(failure_reason=str(exc))

    async def _handle_openai_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type in {"session.created", "session.updated", "conversation.item.created"}:
            log.info("openai_realtime_event", openai_event=event_type)
            return
        if event_type == "input_audio_buffer.speech_started":
            log.info("openai_speech_started")
            await self._clear_twilio_buffer()
            return
        if event_type == "input_audio_buffer.speech_stopped":
            log.info("openai_speech_stopped")
            return
        if event_type in {"response.created", "response.output_item.added", "response.content_part.added"}:
            log.info("openai_response_started", openai_event=event_type, response_id=event.get("response_id") or (event.get("response") or {}).get("id"))
            return
        if event_type in {"response.audio.delta", "response.output_audio.delta"}:
            await self._send_twilio_audio(event.get("delta"))
            return
        if event_type in {"conversation.item.input_audio_transcription.completed", "input_audio_transcription.completed"}:
            self._add_transcript_turn("user", event.get("transcript"))
            return
        if event_type in {"response.audio_transcript.delta", "response.output_audio_transcript.delta"}:
            response_id = event.get("response_id") or "active"
            delta = event.get("delta")
            if delta:
                self.assistant_transcript_parts.setdefault(response_id, []).append(str(delta))
            return
        if event_type in {"response.audio_transcript.done", "response.output_audio_transcript.done"}:
            response_id = event.get("response_id") or "active"
            transcript = event.get("transcript") or "".join(self.assistant_transcript_parts.pop(response_id, []))
            self._add_transcript_turn("assistant", transcript)
            return
        if event_type in {"response.audio.done", "response.output_audio.done"}:
            log.info("openai_response_audio_done", response_id=event.get("response_id"))
            await self._send_twilio_mark(event.get("response_id") or "openai-response")
            return
        if event_type == "response.done":
            log.info("openai_response_completed", response_id=(event.get("response") or {}).get("id"))
            return
        if event_type == "error":
            log.warning("openai_realtime_error", error=event.get("error"))
            return
        log.debug("openai_realtime_event_ignored", openai_event=event_type)

    async def _send_twilio_audio(self, payload: str | None) -> None:
        if not payload or not self.state or not self.state.stream_sid:
            return
        await self.websocket.send_text(json.dumps({"event": "media", "streamSid": self.state.stream_sid, "media": {"payload": payload}}))
        self.metrics.inc("voice_twilio_media_sent_total")

    async def _send_twilio_mark(self, response_id: str) -> None:
        if not self.state or not self.state.stream_sid:
            return
        mark_name = f"openai-{response_id}"[:120]
        await self.websocket.send_text(json.dumps({"event": "mark", "streamSid": self.state.stream_sid, "mark": {"name": mark_name}}))
        log.info("twilio_mark_sent", mark_name=mark_name)

    async def _clear_twilio_buffer(self) -> None:
        if self.state and self.state.stream_sid:
            await self.websocket.send_text(json.dumps({"event": "clear", "streamSid": self.state.stream_sid}))
            self.metrics.inc("voice_twilio_clear_total")
            log.info("twilio_clear_sent")

    async def _send_openai(self, event: dict) -> None:
        if not self.openai_ws:
            raise RuntimeError("OpenAI realtime websocket is not connected")
        await self.openai_ws.send(json.dumps(event))

    async def _stop(self, failure_reason: str | None = None) -> None:
        if self.stopping:
            return
        self.stopping = True
        await self._close_openai()
        if self.state:
            summary = await self._summarize_and_persist_call()
            async with SessionLocal() as session:
                repo = CallRepository(session)
                call = await repo.get_by_sid(self.state.call_sid)
                if call:
                    await repo.finish_call(call, summary, failure_reason)
            await self.memory.delete(self.state.call_sid)
        log.info("call_finished", failure_reason=failure_reason)

    async def _summarize_and_persist_call(self) -> dict[str, Any] | None:
        if not self.state:
            return None
        transcript = self._full_transcript()
        log.info("call_completion_started", transcript_chars=len(transcript), transcript_turns=len(self.transcript_turns))
        summary = await self.summarizer.summarize(transcript)
        payload = self._crm_payload(summary, transcript)
        await self.crm.insert_call(payload)
        log.info("call_completion_saved", lead_status=summary.get("lead_status"))
        return summary

    def _add_transcript_turn(self, role: str, text: str | None) -> None:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return
        self.transcript_turns.append({"role": role, "text": cleaned})
        log.info("transcript_accumulated", role=role, chars=len(cleaned), turns=len(self.transcript_turns))

    def _full_transcript(self) -> str:
        lines = [f"{turn['role'].title()}: {turn['text']}" for turn in self.transcript_turns]
        transcript = "\n".join(lines)
        if len(transcript) > self.settings.max_transcript_chars:
            return transcript[-self.settings.max_transcript_chars :]
        return transcript

    def _crm_payload(self, summary: dict[str, Any], transcript: str) -> dict[str, Any]:
        lead_info = summary.get("lead_info") or {}
        return {
            "call_sid": self.state.call_sid if self.state else self.claims.call_sid,
            "phone_number": self.state.caller_number if self.state else None,
            "caller_name": _text_or_none(lead_info.get("name")),
            "pg_for": _text_or_none(lead_info.get("pg_for")),
            "sharing_preference": _text_or_none(lead_info.get("sharing_preference")),
            "budget": _text_or_none(lead_info.get("budget")),
            "move_in_date": _text_or_none(lead_info.get("move_in_date")),
            "occupation": _text_or_none(lead_info.get("occupation")),
            "whatsapp_confirmation": _bool_or_none(lead_info.get("whatsapp_confirmation")),
            "visit_interest": _text_or_none(lead_info.get("visit_interest")),
            "lead_status": summary.get("lead_status") or "needs_follow_up",
            "sentiment": _text_or_none(summary.get("sentiment")),
            "outcome": _text_or_none(summary.get("outcome")),
            "objections": _text_or_none(_serialize_text(lead_info.get("objections"))),
            "summary": _text_or_none(summary.get("summary")),
            "full_transcript": transcript,
        }

    async def _close_openai(self) -> None:
        if self.openai_ws:
            with contextlib.suppress(Exception):
                await self.openai_ws.close()
            self.openai_ws = None

    def _validate_twilio_audio_format(self, media_format: dict) -> None:
        encoding = str(media_format.get("encoding") or "").lower()
        sample_rate = str(media_format.get("sampleRate") or "")
        channels = str(media_format.get("channels") or "1")
        if "mulaw" not in encoding or sample_rate != "8000" or channels != "1":
            self.metrics.inc("voice_bad_twilio_media_format_total")
            raise ValueError(f"unsupported Twilio media format: {media_format}")

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _cancel_tasks(self) -> None:
        tasks = [task for task in self.tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self.settings.provider_cleanup_timeout_seconds)


def _realtime_instructions() -> str:
    return (
        SYSTEM_PROMPT
        + "\n\nDemo call rules:\n"
        + "- Speak very clearly, slowly, and briefly.\n"
        + "- Use simple Hindi-English Roman speech.\n"
        + "- Sound like a confident PG admissions counsellor, not a generic assistant.\n"
        + "- Mention one useful benefit when answering: food included, safe setup, clean rooms, or convenient location.\n"
        + "- Do not repeat the same question if the user already answered.\n"
        + "- If audio is unclear, ask one short clarification.\n"
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"yes", "true", "haan", "ha", "ji", "confirmed"}:
        return True
    if text in {"no", "false", "nahi", "nahin", "not confirmed"}:
        return False
    return None


def _serialize_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip()) or None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
