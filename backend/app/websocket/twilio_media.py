import asyncio
import base64
import contextlib
import inspect
import json
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app.conversation.anti_repetition import can_ask, infer_asked_field, record_question
from app.conversation.customer_profile import classify_customer_profile_with_confidence, get_profile_context
from app.conversation.fact_extractor import extract_customer_facts
from app.conversation.language_manager import detect_language_update, get_language_context
from app.conversation.lead_scoring import calculate_lead_score, get_lead_score_context
from app.conversation.memory_manager import CallMemoryManager, build_memory_context
from app.conversation.objection_detector import detect_objections, get_objection_context, merge_objections
import app.conversation.persona as persona_module
from app.conversation.persona import get_persona_context
from app.conversation.project_matcher import project_context
from app.conversation.quality_metrics import assistant_turn_metrics, user_turn_metrics
from app.conversation.stage_manager import determine_stage_with_reason, get_stage_context
from app.database.session import SessionLocal
from app.models import Call
from app.prompts.real_estate_agent import (
    SYSTEM_PROMPT,
    OUTGOING_CONFIRM_LINE,
    OUTGOING_INTRO_LINE,
    build_dynamic_response_context,
)
from app.observability import Metrics
from app.services.call_repository import CallRepository
from app.services.call_summary import CallSummarizer
from app.services.cartesia_tts import CartesiaTTS
from app.services.crm_outbox import CRMOutbox
from app.services.memory import ConversationMemory, ConversationState
from app.services.supabase_crm import SupabaseCRM
from app.telephony.stream_auth import StreamClaims, StreamTokenService
from app.utils.logging import call_sid_ctx, log


OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
GREETING_LOCK_SECONDS = 4.0  # was 2.2 — greeting ~3-4s, old value unlocked before completion
BARGE_IN_DEBOUNCE_SECONDS = 0.35
RESPONSE_AUDIO_TIMEOUT_SECONDS = 2.5
CARTESIA_RESPONSE_TIMEOUT_SECONDS = 4.0
SILENCE_PROMPT_SECONDS = 8.0
MAX_SILENCE_PROMPTS = 2
MAX_CALL_SECONDS = 210.0
OPENAI_CONNECT_TIMEOUT_SECONDS = 4.0
SESSION_UPDATE_ACK_TIMEOUT_SECONDS = 2.0
EARLY_AUDIO_BUFFER_FRAMES = 300
TWILIO_OUTBOUND_QUEUE_SIZE = 1000
OPENAI_HEARTBEAT_SECONDS = 20.0
OPENAI_PONG_TIMEOUT_SECONDS = 10.0
OPENAI_RECONNECT_ATTEMPTS = 3
CRITICAL_BACKGROUND_TASKS = {
    "twilio_outbound_sender",
    "openai_realtime_connector",
    "openai_realtime_receiver",
}
UNSUPPORTED_AREA_RESPONSE = (
    "Woh area hamare paas covered nahi hai abhi. Hamare projects Greater Noida West mein hain — "
    "agar kabhi us side consider karo toh batana."
)
AI_IDENTITY_RESPONSE_HINDI = "Nahi sir, main Riya hoon DreamHome se."
AI_IDENTITY_RESPONSE_ENGLISH = "No sir, this is Riya from DreamHome."
CALL_REASON_RESPONSE = "Aapki property enquiry receive hui thi sir, isi liye follow-up call tha."
CALL_REASON_TERMS = (
    "call kyu",
    "call kyun",
    "call kyon",
    "kyu call",
    "kyun call",
    "kyon call",
    "kyu kiya",
    "kyun kiya",
    "kyon kiya",
    "call kyun kiya",
    "call kyon kiya",
    "why did you call",
    "why have you called",
    "आपने call क्यों",
    "call क्यों किया",
    "क्यों call",
    "क्यों किया",
    "किसलिए call",
)
UNSUPPORTED_INVENTORY_TERMS = (
    "sector 62",
    "sector-62",
    "electronic city",
    "saya gold",
    "saya gold avenue",
    "jaipuriya",
    "jaipuriya sunrise",
    "jaipuriya sunrise greens",
    "purvanchal heights",
    "mahagun",
    "mahagun mansion",
)
AI_IDENTITY_TERMS = (
    "ai bol",
    "ai ho",
    "ai lag",
    "artificial intelligence",
    "robot",
    "bot",
    "एआई",
    "ए आई",
    "AI बोल",
)


class CallPhase(StrEnum):
    NEW = "NEW"
    TWILIO_CONNECTED = "TWILIO_CONNECTED"
    STREAM_STARTED = "STREAM_STARTED"
    OPENAI_CONNECTING = "OPENAI_CONNECTING"
    OPENAI_CONNECTED = "OPENAI_CONNECTED"
    SESSION_UPDATING = "SESSION_UPDATING"
    SESSION_READY = "SESSION_READY"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    USER_SPEAKING = "USER_SPEAKING"
    PROCESSING_USER = "PROCESSING_USER"
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"
    CALL_ENDING = "CALL_ENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GreetingState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PLAYING = "PLAYING"
    COMPLETED = "COMPLETED"


@dataclass
class GreetingManager:
    state: GreetingState = GreetingState.NOT_STARTED
    response_id: str | None = None
    started_at: float | None = None
    completed_at: float | None = None

    def start(self, response_id: str) -> None:
        if self.state == GreetingState.COMPLETED:
            return
        self.state = GreetingState.PLAYING
        self.response_id = response_id
        self.started_at = monotonic()
        self.completed_at = None

    def complete(self, response_id: str | None = None) -> bool:
        if self.state != GreetingState.PLAYING:
            return False
        if response_id and self.response_id and response_id != self.response_id:
            return False
        self.state = GreetingState.COMPLETED
        self.completed_at = monotonic()
        return True

    @property
    def is_playing(self) -> bool:
        return self.state == GreetingState.PLAYING

    @property
    def is_completed(self) -> bool:
        return self.state == GreetingState.COMPLETED


@dataclass
class ResponseManager:
    generation: int = 0
    response_id: str | None = None
    playback_id: str | None = None
    cancellation_reason: str | None = None

    def begin_response(self, response_id: str | None) -> None:
        self.response_id = response_id
        self.cancellation_reason = None

    def begin_playback(self, playback_id: str | None) -> int:
        self.generation += 1
        self.playback_id = playback_id
        self.cancellation_reason = None
        return self.generation

    def is_current(self, generation: int, playback_id: str | None = None) -> bool:
        if generation != self.generation:
            return False
        return playback_id is None or self.playback_id == playback_id

    def cancel(self, reason: str) -> None:
        self.generation += 1
        self.response_id = None
        self.playback_id = None
        self.cancellation_reason = reason


ALLOWED_PHASE_TRANSITIONS: dict[CallPhase, set[CallPhase]] = {
    CallPhase.NEW: {CallPhase.TWILIO_CONNECTED, CallPhase.FAILED},
    CallPhase.TWILIO_CONNECTED: {CallPhase.STREAM_STARTED, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.STREAM_STARTED: {
        CallPhase.OPENAI_CONNECTING,
        CallPhase.SESSION_READY,
        CallPhase.ASSISTANT_SPEAKING,
        CallPhase.USER_SPEAKING,
        CallPhase.PROCESSING_USER,
        CallPhase.WAITING_FOR_USER,
        CallPhase.CALL_ENDING,
        CallPhase.FAILED,
    },
    CallPhase.OPENAI_CONNECTING: {CallPhase.OPENAI_CONNECTED, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.OPENAI_CONNECTED: {CallPhase.SESSION_UPDATING, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.SESSION_UPDATING: {CallPhase.SESSION_READY, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.SESSION_READY: {CallPhase.OPENAI_CONNECTING, CallPhase.ASSISTANT_SPEAKING, CallPhase.USER_SPEAKING, CallPhase.WAITING_FOR_USER, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.WAITING_FOR_USER: {CallPhase.OPENAI_CONNECTING, CallPhase.USER_SPEAKING, CallPhase.PROCESSING_USER, CallPhase.ASSISTANT_SPEAKING, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.USER_SPEAKING: {CallPhase.OPENAI_CONNECTING, CallPhase.PROCESSING_USER, CallPhase.ASSISTANT_SPEAKING, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.PROCESSING_USER: {CallPhase.OPENAI_CONNECTING, CallPhase.ASSISTANT_SPEAKING, CallPhase.WAITING_FOR_USER, CallPhase.USER_SPEAKING, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.ASSISTANT_SPEAKING: {CallPhase.WAITING_FOR_USER, CallPhase.USER_SPEAKING, CallPhase.PROCESSING_USER, CallPhase.CALL_ENDING, CallPhase.FAILED},
    CallPhase.CALL_ENDING: {CallPhase.COMPLETED, CallPhase.FAILED},
    CallPhase.COMPLETED: set(),
    CallPhase.FAILED: set(),
}


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
        crm_outbox: CRMOutbox,
    ):
        self.websocket = websocket
        self.settings = settings
        self.memory = memory
        self.stream_tokens = stream_tokens
        self.claims = claims
        self.metrics = metrics
        self.summarizer = summarizer
        self.crm = crm
        self.crm_outbox = crm_outbox
        self.call_memory = CallMemoryManager(memory.redis)
        self.cartesia_tts = CartesiaTTS(settings) if settings.tts_provider == "cartesia" else None

        self.state: ConversationState | None = None
        self.call: Call | None = None
        self.openai_ws = None
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.stopping = False
        self.transcript_turns: list[dict[str, str]] = []
        self.assistant_transcript_parts: dict[str, list[str]] = {}
        self.openai_audio_fallback_parts: dict[str, list[str]] = {}
        self.cartesia_tts_active: set[str] = set()
        self.cartesia_playback_started: set[str] = set()
        self.cartesia_spoken_response_ids: set[str] = set()
        self.greeting_manager = GreetingManager()
        self.response_manager = ResponseManager()
        self.greeting_clear_locked_until = 0.0
        self.greeting_response_id: str | None = None
        self.greeting_completed = False
        self.greeting_warmup_chunks_to_drop = 3
        self.greeting_warmup_chunks_dropped = 0
        self.assistant_response_active = False
        self.assistant_speaking = False
        self.current_response_id: str | None = None
        self.response_started_at: float | None = None
        self.response_completed_at: float | None = None
        self.response_first_audio_at: float | None = None
        self.response_audio_sent_counts: dict[str, int] = {}
        self.twilio_media_sent_count = 0
        self.pending_marks: set[str] = set()
        self.mark_response_ids: dict[str, str] = {}
        self.caller_speaking = False
        self.caller_speech_started_at: float | None = None
        self.last_activity_at = monotonic()
        self.last_assistant_audio_at = 0.0
        self.silence_prompt_count = 0
        self.close_after_current_response = False
        self._barge_in_task: asyncio.Task | None = None
        self.customer_memory: dict[str, Any] = {}
        self.call_started_at: float | None = None
        self.openai_connect_started_at: float | None = None
        self.openai_tcp_connected_at: float | None = None
        self.openai_ws_connected_at: float | None = None
        self.openai_connected_at: float | None = None
        self.session_update_sent_at: float | None = None
        self.session_update_ack_at: float | None = None
        self.session_updated_at: float | None = None
        self.response_create_sent_at: float | None = None
        self.pending_response_create_reason: str | None = None
        self.greeting_response_create_at: float | None = None
        self.first_audio_delta_at: float | None = None
        self.openai_output_audio_format: str | None = None
        self.openai_output_audio_rate: int | None = None
        self.active_realtime_voice = self.settings.openai_realtime_voice
        self.voice_fallback_applied = False
        self.openai_audio_delta_count = 0
        self.deferred_response_instructions: str | None = None
        self.deferred_response_reason: str | None = None
        self.session_update_ack_event = asyncio.Event()
        self.openai_ready = False
        self.early_audio_frames: deque[str] = deque(maxlen=EARLY_AUDIO_BUFFER_FRAMES)
        self.twilio_outbound_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=TWILIO_OUTBOUND_QUEUE_SIZE)
        self.openai_reconnect_attempts = 0
        self.openai_connect_task: asyncio.Task | None = None
        self.openai_recovering = False
        self._transcript_lock = asyncio.Lock()
        self.phase = CallPhase.NEW

    async def run(self) -> None:
        await self.websocket.accept()
        self._transition(CallPhase.TWILIO_CONNECTED, "twilio_websocket_accepted")
        self.metrics.inc("voice_ws_connected_total")
        self._spawn(self._twilio_sender_loop(), "twilio_outbound_sender")
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
            await self._handle_twilio_mark(message)
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
        self.call_started_at = monotonic()
        call_sid_ctx.set(call_sid)
        self._transition(CallPhase.STREAM_STARTED, "twilio_start_received")

        self.state = await self.memory.get(call_sid) or ConversationState(call_sid=call_sid)
        self.state.stream_sid = stream_sid
        self.state.caller_number = caller
        await self.memory.set(self.state)
        self.customer_memory = await self.call_memory.load_memory(call_sid)

        async with SessionLocal() as session:
            repo = CallRepository(session)
            self.call = await repo.start_call(call_sid, stream_sid, caller)

        log.info("call_started", stream_sid=stream_sid, media_format=media_format, provider="openai_realtime")
        log.info("call_connected", call_sid=call_sid, stream_sid=stream_sid)
        log.info(
            "voice_output_provider",
            provider=self.settings.tts_provider,
            cartesia_enabled=self.cartesia_tts is not None,
            cartesia_model=self.settings.cartesia_model_id if self.cartesia_tts else None,
            cartesia_voice_id_suffix=self.settings.cartesia_voice_id[-6:] if self.cartesia_tts and self.settings.cartesia_voice_id else None,
        )
        if self.cartesia_tts:
            log.info(
                "cartesia_ready",
                model=self.settings.cartesia_model_id,
                voice_id_suffix=self.settings.cartesia_voice_id[-6:] if self.settings.cartesia_voice_id else None,
                sample_rate=self.settings.cartesia_sample_rate,
                encoding=self.settings.cartesia_encoding,
            )
        self.openai_connect_task = self._spawn(self._connect_openai(request_greeting=True), "openai_realtime_connector")
        self._spawn(self._silence_watchdog(), "silence_watchdog")

    async def _connect_openai(self, *, request_greeting: bool) -> None:
        self._transition(CallPhase.OPENAI_CONNECTING, "openai_connect_started")
        url = f"{OPENAI_REALTIME_URL}?model={self.settings.openai_realtime_model}"
        self.session_update_ack_event = asyncio.Event()
        self.openai_ready = False
        self.openai_connect_started_at = monotonic()
        log.info(
            "openai_connect_started",
            model=self.settings.openai_realtime_model,
            call_to_connect_start_ms=self._elapsed_ms(self.call_started_at, self.openai_connect_started_at),
        )
        log.info("openai_realtime_connecting", model=self.settings.openai_realtime_model)
        try:
            self.openai_ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers={
                        "Authorization": f"Bearer {self.settings.openai_api_key}",
                        "OpenAI-Safety-Identifier": self.claims.call_sid,
                    },
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=2**23,
                ),
                timeout=OPENAI_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.exception(
                "openai_connect_failed",
                error=str(exc),
                timeout_seconds=OPENAI_CONNECT_TIMEOUT_SECONDS,
                call_to_failure_ms=self._elapsed_ms(self.call_started_at, monotonic()),
            )
            self.metrics.inc("voice_openai_connect_failed_total")
            await self._stop(failure_reason=f"openai connect failed: {exc.__class__.__name__}")
            return
        self.openai_tcp_connected_at = monotonic()
        log.info(
            "openai_tcp_connected",
            connect_duration_ms=self._elapsed_ms(self.openai_connect_started_at, self.openai_tcp_connected_at),
            call_to_tcp_connected_ms=self._elapsed_ms(self.call_started_at, self.openai_tcp_connected_at),
        )
        self.openai_ws_connected_at = monotonic()
        self.openai_connected_at = self.openai_ws_connected_at
        log.info(
            "openai_ws_connected",
            connect_duration_ms=self._elapsed_ms(self.openai_connect_started_at, self.openai_ws_connected_at),
            tcp_to_ws_ms=self._elapsed_ms(self.openai_tcp_connected_at, self.openai_ws_connected_at),
            call_to_ws_connected_ms=self._elapsed_ms(self.call_started_at, self.openai_ws_connected_at),
        )
        connect_ms = self._elapsed_ms(self.openai_connect_started_at, self.openai_ws_connected_at)
        if connect_ms is not None:
            self.metrics.observe_ms("openai_connect", connect_ms)
        if self.call_started_at:
            log.info("openai_connected_latency_ms", latency_ms=round((self.openai_connected_at - self.call_started_at) * 1000, 2))
        log.info(
            "openai_realtime_connected",
            model=self.settings.openai_realtime_model,
            voice=self.active_realtime_voice,
            speed=self.settings.openai_realtime_speed,
        )
        self._transition(CallPhase.OPENAI_CONNECTED, "openai_ws_connected")
        self._spawn(self._openai_loop(), "openai_realtime_receiver")
        self._spawn(self._openai_heartbeat_loop(), "openai_heartbeat")
        self.session_update_sent_at = monotonic()
        self._transition(CallPhase.SESSION_UPDATING, "session_update_sent")
        language = self.customer_memory.get("language", "hinglish")
        session_instructions = _realtime_instructions(language)
        persona_context = get_persona_context(language)
        log.info(
            "persona_context_loaded",
            persona_source_file=inspect.getsourcefile(persona_module),
            chars=len(persona_context),
            preview=_preview(persona_context),
        )
        log.info("session_update_instructions_preview", chars=len(session_instructions), preview=_preview(session_instructions))
        await self._send_session_update(session_instructions, reason="initial")
        log.info(
            "session_update_sent",
            ws_to_session_update_sent_ms=self._elapsed_ms(self.openai_ws_connected_at, self.session_update_sent_at),
            call_to_session_update_sent_ms=self._elapsed_ms(self.call_started_at, self.session_update_sent_at),
        )
        self.session_updated_at = monotonic()
        if self.call_started_at:
            log.info(
                "openai_session_update_latency_ms",
                latency_ms=round((self.session_updated_at - self.call_started_at) * 1000, 2),
            )
        log.info("openai_realtime_session_update_sent")
        try:
            await asyncio.wait_for(self.session_update_ack_event.wait(), timeout=SESSION_UPDATE_ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning(
                "session_update_ack_timeout",
                timeout_seconds=SESSION_UPDATE_ACK_TIMEOUT_SECONDS,
                call_to_timeout_ms=self._elapsed_ms(self.call_started_at, monotonic()),
            )
        session_update_ms = self._elapsed_ms(self.session_update_sent_at, self.session_update_ack_at)
        if session_update_ms is not None:
            self.metrics.observe_ms("session_update", session_update_ms)
        self.openai_ready = True
        self._transition(CallPhase.SESSION_READY, "session_ready")
        await self._flush_early_audio()
        if request_greeting:
            if self._using_cartesia_tts():
                greeting_text = self._greeting_text()
                if not self._greeting_has_customer_name():
                    self.customer_memory = await self.call_memory.update_memory(
                        self.state.call_sid,
                        {"intro_delivered": True, "conversation_stage": "INTRO"},
                    )
                await self._speak_fixed_text(greeting_text, response_id="cartesia-greeting", reason="greeting")
            else:
                self.greeting_manager.start("openai-greeting-pending")
                self.greeting_clear_locked_until = monotonic() + GREETING_LOCK_SECONDS
                log.info("greeting_started", mode="openai_audio", lock_seconds=GREETING_LOCK_SECONDS)
                await self._request_assistant_response(
                    self._build_greeting_instructions(),
                    reason="greeting",
                    force=True,
                )

    async def _media(self, message: dict) -> None:
        self.last_activity_at = monotonic()
        payload = (message.get("media") or {}).get("payload")
        if not payload:
            return
        try:
            base64.b64decode(payload, validate=True)
        except Exception:
            self.metrics.inc("voice_bad_media_payload_total")
            return
        if not self.openai_ws or not self.openai_ready:
            before = len(self.early_audio_frames)
            self.early_audio_frames.append(payload)
            if before == self.early_audio_frames.maxlen:
                self.metrics.inc("voice_early_audio_dropped_total")
                log.warning("early_audio_frame_dropped", buffered_frames=len(self.early_audio_frames))
            else:
                self.metrics.gauge("voice_early_audio_buffered_frames", len(self.early_audio_frames))
                if len(self.early_audio_frames) == 1 or len(self.early_audio_frames) % 50 == 0:
                    log.info("early_audio_frame_buffered", buffered_frames=len(self.early_audio_frames))
            return
        await self._send_openai({"type": "input_audio_buffer.append", "audio": payload})
        self.metrics.inc("voice_twilio_media_frames_total")

    async def _flush_early_audio(self) -> None:
        if not self.openai_ws or not self.openai_ready or not self.early_audio_frames:
            return
        frames = len(self.early_audio_frames)
        log.info("early_audio_flush_started", frames=frames)
        while self.early_audio_frames and self.openai_ws and self.openai_ready and not self.stopping:
            await self._send_openai({"type": "input_audio_buffer.append", "audio": self.early_audio_frames.popleft()})
            self.metrics.inc("voice_twilio_media_frames_total")
        self.metrics.gauge("voice_early_audio_buffered_frames", len(self.early_audio_frames))
        log.info("early_audio_flush_completed", frames=frames, remaining=len(self.early_audio_frames))

    async def _openai_loop(self) -> None:
        if not self.openai_ws:
            return
        try:
            async for raw in self.openai_ws:
                event = json.loads(raw)
                await self._handle_openai_event(event)
        except ConnectionClosed as exc:
            if not self.stopping:
                log.warning("openai_realtime_closed", code=exc.code, reason=exc.reason, phase=self.phase.value)
                self.metrics.inc("openai_disconnect_total")
                if exc.code == 1006 and "ping" in str(exc.reason).lower():
                    self.metrics.inc("websocket_ping_timeout_total")
                await self._recover_openai_connection(f"closed:{exc.code}")
        except Exception as exc:
            if not self.stopping:
                log.exception("openai_realtime_receiver_failed", error=str(exc))
                self.metrics.inc("openai_disconnect_total")
                await self._recover_openai_connection(exc.__class__.__name__)

    async def _handle_openai_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "session.updated":
            self.session_update_ack_at = monotonic()
            self.session_update_ack_event.set()
            self._record_openai_audio_format(event)
            log.info(
                "session_update_ack",
                session_update_rtt_ms=self._elapsed_ms(self.session_update_sent_at, self.session_update_ack_at),
                ws_to_session_update_ack_ms=self._elapsed_ms(self.openai_ws_connected_at, self.session_update_ack_at),
                call_to_session_update_ack_ms=self._elapsed_ms(self.call_started_at, self.session_update_ack_at),
            )
            log.info("openai_realtime_event", openai_event=event_type)
            return
        if event_type == "session.created":
            session = event.get("session") or {}
            audio = session.get("audio") or {}
            log.info(
                "session_created",
                model=self.settings.openai_realtime_model,
                requested_voice=self.settings.openai_realtime_voice,
                active_voice=self.active_realtime_voice,
                input_format=(((audio.get("input") or {}).get("format") or {}).get("type")),
                output_format=(((audio.get("output") or {}).get("format") or {}).get("type")),
            )
            return
        if event_type == "conversation.item.created":
            log.info("openai_realtime_event", openai_event=event_type)
            return
        if event_type == "input_audio_buffer.speech_started":
            self.last_activity_at = monotonic()
            if self._greeting_locked():
                log.info("interruption_ignored_during_greeting")
                return
            if self.phase in {CallPhase.OPENAI_CONNECTING, CallPhase.OPENAI_CONNECTED, CallPhase.SESSION_UPDATING}:
                log.info("speech_started_ignored_before_session_ready", phase=self.phase.value)
                return
            self.caller_speaking = True
            self.caller_speech_started_at = monotonic()
            self._safe_transition(CallPhase.USER_SPEAKING, "openai_speech_started")
            log.info("openai_speech_started")
            if self._barge_in_task and not self._barge_in_task.done():
                return
            self._barge_in_task = self._spawn(self._confirm_barge_in_after_delay(), "barge_in_debounce")
            return
        if event_type == "input_audio_buffer.speech_stopped":
            self.last_activity_at = monotonic()
            self.caller_speaking = False
            self.caller_speech_started_at = None
            log.info("openai_speech_stopped")
            if not self.assistant_response_active and not self.assistant_speaking and not self.pending_marks:
                await self._flush_deferred_response()
            return
        if event_type == "response.created":
            self._handle_response_started(event)
            return
        if event_type in {"response.output_item.added", "response.content_part.added"}:
            log.info("openai_response_event", openai_event=event_type, response_id=event.get("response_id") or (event.get("response") or {}).get("id"))
            return
        if event_type in {"response.content_part.done", "response.output_item.done"}:
            response_id = event.get("response_id") or self.current_response_id or "active"
            text = self._extract_text_from_response_event(event)
            if text:
                self._handle_assistant_text_completed(response_id, text, source=event_type)
            else:
                log.info("openai_text_event_without_text", openai_event=event_type, response_id=response_id)
            return
        if event_type in {"response.audio.delta", "response.output_audio.delta"}:
            self.openai_audio_delta_count += 1
            if self._using_cartesia_tts():
                response_id = event.get("response_id") or self.current_response_id or "active"
                if self.response_first_audio_at is None:
                    self.response_first_audio_at = monotonic()
                if self.openai_audio_delta_count == 1:
                    log.error(
                        "openai_audio_delta_unexpected_in_cartesia_mode",
                        response_id=response_id,
                        output_format=self.openai_output_audio_format,
                        action="ignored_never_play_openai_audio",
                    )
                return
            if self.openai_audio_delta_count == 1 or self.openai_audio_delta_count % 50 == 0:
                delta = event.get("delta") or ""
                log.info(
                    "response_audio_delta",
                    response_id=event.get("response_id"),
                    delta_count=self.openai_audio_delta_count,
                    payload_chars=len(delta),
                    output_format=self.openai_output_audio_format,
                )
            await self._send_twilio_audio(event.get("delta"), response_id=event.get("response_id"))
            return
        if event_type in {"conversation.item.input_audio_transcription.completed", "input_audio_transcription.completed"}:
            transcript = event.get("transcript")
            self._spawn(self._handle_user_transcript(transcript), "user_transcript_handler")
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
            self._handle_assistant_text_completed(response_id, transcript, source=event_type)
            return
        if event_type in {"response.text.delta", "response.output_text.delta"}:
            response_id = event.get("response_id") or self.current_response_id or "active"
            delta = event.get("delta")
            if delta:
                self.assistant_transcript_parts.setdefault(response_id, []).append(str(delta))
            return
        if event_type in {"response.text.done", "response.output_text.done"}:
            response_id = event.get("response_id") or self.current_response_id or "active"
            transcript = (
                event.get("text")
                or event.get("transcript")
                or "".join(self.assistant_transcript_parts.pop(response_id, []))
            )
            self._handle_assistant_text_completed(response_id, transcript, source=event_type)
            return
        if event_type in {"response.audio.done", "response.output_audio.done"}:
            response_id = event.get("response_id") or self.current_response_id or "openai-response"
            if self._using_cartesia_tts():
                log.info(
                    "openai_audio_done_ignored_for_cartesia",
                    response_id=response_id,
                    fallback_chunks=len(self.openai_audio_fallback_parts.get(response_id, [])),
                )
                return
            response_media_sent = self.response_audio_sent_counts.pop(response_id, 0)
            log.info(
                "assistant_audio_finished",
                response_id=response_id,
                media_sent=self.twilio_media_sent_count,
                response_media_sent=response_media_sent,
            )
            if response_media_sent:
                await self._send_twilio_mark(response_id)
            else:
                log.info("assistant_audio_done_no_twilio_audio", response_id=response_id)
            return
        if event_type == "response.done":
            response_id = event.get("response_id") or (event.get("response") or {}).get("id")
            if self._using_cartesia_tts() and response_id and response_id not in self.cartesia_spoken_response_ids:
                response_text = self._extract_text_from_response_event(event.get("response") or event)
                if response_text:
                    self._handle_assistant_text_completed(response_id, response_text, source=event_type)
            log.info(
                "response_done",
                response_id=response_id,
                status=(event.get("response") or {}).get("status"),
                audio_delta_count=self.openai_audio_delta_count,
            )
            self._handle_response_completed(event)
            return
        if event_type == "error":
            log.warning("openai_realtime_error", error=event.get("error"), active_voice=self.active_realtime_voice)
            await self._maybe_fallback_realtime_voice(event)
            return
        log.debug("openai_realtime_event_ignored", openai_event=event_type)

    async def _send_twilio_audio(self, payload: str | None, *, response_id: str | None = None) -> int:
        if not payload or not self.state or not self.state.stream_sid:
            return 0
        if (
            response_id == getattr(self, "greeting_response_id", None)
            and not getattr(self, "greeting_completed", False)
            and getattr(self, "greeting_warmup_chunks_dropped", 0) < getattr(self, "greeting_warmup_chunks_to_drop", 0)
        ):
            self.greeting_warmup_chunks_dropped += 1
            log.info(
                "greeting_warmup_chunk_dropped",
                response_id=response_id,
                dropped_count=self.greeting_warmup_chunks_dropped,
                drop_limit=self.greeting_warmup_chunks_to_drop,
            )
            return 0
        if self.caller_speaking or self.phase == CallPhase.USER_SPEAKING:
            self.metrics.inc("voice_stale_audio_skipped_total")
            log.info("stale_audio_skipped", response_id=response_id, reason="caller_speaking")
            return 0
        payloads = self._twilio_safe_audio_payloads(payload, response_id=response_id)
        if not payloads:
            return 0
        sent_count = 0
        for safe_payload in payloads:
            if self.twilio_media_sent_count == 0 and sent_count == 0:
                try:
                    twilio_bytes = len(base64.b64decode(safe_payload, validate=True))
                except Exception:
                    twilio_bytes = None
                log.info(
                    "twilio_audio_first_payload",
                    response_id=response_id,
                    bytes=twilio_bytes,
                    source_format=self.openai_output_audio_format,
                    source_rate=self.openai_output_audio_rate,
                    transcoded=False,
                )
            if self._enqueue_twilio_message({"event": "media", "streamSid": self.state.stream_sid, "media": {"payload": safe_payload}}, "audio"):
                sent_count += 1
        if sent_count == 0:
            return 0
        if not self.assistant_speaking:
            self._set_assistant_speaking(True, "first_audio_delta")
            self._safe_transition(CallPhase.ASSISTANT_SPEAKING, "first_audio_delta")
            self.response_first_audio_at = monotonic()
            if self.first_audio_delta_at is None:
                self.first_audio_delta_at = self.response_first_audio_at
            log.info(
                "first_audio_delta",
                response_id=self.current_response_id,
                response_create_to_first_audio_ms=self._elapsed_ms(self.response_create_sent_at, self.response_first_audio_at),
                session_ack_to_first_audio_ms=self._elapsed_ms(self.session_update_ack_at, self.response_first_audio_at),
                ws_to_first_audio_ms=self._elapsed_ms(self.openai_ws_connected_at, self.response_first_audio_at),
                call_to_first_audio_ms=self._elapsed_ms(self.call_started_at, self.response_first_audio_at),
            )
            first_audio_ms = self._elapsed_ms(self.call_started_at, self.response_first_audio_at)
            if first_audio_ms is not None:
                self.metrics.observe_ms("first_audio", first_audio_ms)
            log.info("assistant_audio_first_delta", response_id=self.current_response_id)
        self.twilio_media_sent_count += sent_count
        response_key = response_id or self.current_response_id
        if response_key:
            self.response_audio_sent_counts[response_key] = self.response_audio_sent_counts.get(response_key, 0) + sent_count
        self.last_assistant_audio_at = monotonic()
        for _ in range(sent_count):
            self.metrics.inc("voice_twilio_media_sent_total")
        return sent_count

    def _record_openai_audio_format(self, event: dict) -> None:
        output = (((event.get("session") or {}).get("audio") or {}).get("output") or {})
        audio_format = output.get("format") or {}
        self.openai_output_audio_format = str(audio_format.get("type") or "").lower() or None
        rate = audio_format.get("rate")
        self.openai_output_audio_rate = int(rate) if str(rate or "").isdigit() else None
        log.info(
            "openai_audio_format_verified",
            output_format=self.openai_output_audio_format,
            output_rate=self.openai_output_audio_rate,
            twilio_passthrough=self.openai_output_audio_format == "audio/pcmu",
            cartesia_text_only=self._using_cartesia_tts() and self.openai_output_audio_format is None,
            transcoding_to_twilio=False,
        )
        if self.openai_output_audio_format and self.openai_output_audio_format != "audio/pcmu":
            log.warning(
                "openai_audio_format_not_twilio_native",
                output_format=self.openai_output_audio_format,
                output_rate=self.openai_output_audio_rate,
                action="drop_non_pcmu_audio",
            )

    def _twilio_safe_audio_payloads(self, payload: str, *, response_id: str | None = None) -> list[str]:
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            self.metrics.inc("voice_bad_media_payload_total")
            log.warning("openai_audio_payload_invalid", response_id=response_id)
            return []

        if self.twilio_media_sent_count == 0:
            log.info(
                "openai_audio_first_payload",
                response_id=response_id,
                bytes=len(raw),
                output_format=self.openai_output_audio_format,
                output_rate=self.openai_output_audio_rate,
            )

        if self.openai_output_audio_format and self.openai_output_audio_format != "audio/pcmu":
            self.metrics.inc("voice_codec_mismatch_audio_dropped_total")
            log.error(
                "codec_mismatch_audio_dropped",
                response_id=response_id,
                output_format=self.openai_output_audio_format,
                output_rate=self.openai_output_audio_rate,
                expected_format="audio/pcmu",
                bytes=len(raw),
            )
            return []

        return [payload]

    def _extract_text_from_response_event(self, event: dict) -> str:
        candidates: list[str] = []
        for key in ("text", "transcript", "delta"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
        part = event.get("part") or {}
        if isinstance(part, dict):
            for key in ("text", "transcript"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
        item = event.get("item") or {}
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                for key in ("text", "transcript"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value)
        output = event.get("output") if isinstance(event, dict) else None
        if isinstance(output, list):
            for output_item in output:
                if not isinstance(output_item, dict):
                    continue
                for entry in output_item.get("content") or []:
                    if not isinstance(entry, dict):
                        continue
                    for key in ("text", "transcript"):
                        value = entry.get(key)
                        if isinstance(value, str) and value.strip():
                            candidates.append(value)
        return " ".join(" ".join(candidates).split())

    def _handle_assistant_text_completed(self, response_id: str, transcript: str | None, *, source: str) -> None:
        cleaned = " ".join(str(transcript or "").split())
        if not cleaned:
            log.info("assistant_text_empty", response_id=response_id, source=source)
            return
        if self._using_cartesia_tts() and response_id in self.cartesia_spoken_response_ids:
            log.info("assistant_text_duplicate_ignored", response_id=response_id, source=source)
            return
        if self._add_transcript_turn("assistant", cleaned):
            self._spawn(self._record_assistant_question(cleaned), "assistant_question_recorder")
            self._spawn(self._record_assistant_metrics(cleaned), "assistant_metrics_recorder")
            if self._using_cartesia_tts():
                self.cartesia_spoken_response_ids.add(response_id)
                log.info("cartesia_playback_requested", response_id=response_id, source=source, chars=len(cleaned))
                self._spawn(self._speak_with_cartesia(cleaned, response_id), "cartesia_tts_playback")

    def _using_cartesia_tts(self) -> bool:
        return getattr(self, "cartesia_tts", None) is not None

    async def _speak_fixed_text(self, text: str, *, response_id: str, reason: str) -> None:
        cleaned = " ".join(str(text or "").split())
        if not cleaned or not self.cartesia_tts or not self.state or self.stopping:
            return
        if reason == "greeting":
            self.greeting_response_id = response_id
            self.greeting_manager.start(response_id)
            self.greeting_clear_locked_until = float("inf")
            log.info("greeting_started", mode="cartesia", response_id=response_id)
        playback_generation = self.response_manager.begin_playback(response_id)
        self.cartesia_tts_active.add(response_id)
        self._add_transcript_turn("assistant", cleaned)
        started = monotonic()
        log.info("cartesia_fixed_tts_started", reason=reason, response_id=response_id, chars=len(cleaned))
        try:
            ulaw_audio = await self.cartesia_tts.synthesize_ulaw(cleaned, call_sid=self.state.call_sid)
        except Exception as exc:
            log.exception("cartesia_fixed_tts_failed", reason=reason, response_id=response_id, error=str(exc))
            self.metrics.inc("cartesia_tts_failure_total")
            self.cartesia_tts_active.discard(response_id)
            if reason == "greeting":
                self.greeting_completed = True
                self.greeting_manager.complete(response_id)
                self.greeting_clear_locked_until = 0.0
            return
        if not self.response_manager.is_current(playback_generation, response_id):
            self.cartesia_tts_active.discard(response_id)
            log.info("cartesia_fixed_tts_discarded", reason=reason, response_id=response_id, discard_reason="stale_generation")
            return
        self.metrics.inc("cartesia_tts_success_total")
        log.info(
            "cartesia_fixed_tts_ready",
            reason=reason,
            response_id=response_id,
            latency_ms=self._elapsed_ms(started, monotonic()),
            audio_bytes=len(ulaw_audio),
        )
        try:
            await self._send_twilio_ulaw_audio(ulaw_audio, response_id=response_id)
        finally:
            self.cartesia_tts_active.discard(response_id)

    async def _speak_with_cartesia(self, transcript: str | None, response_id: str | None) -> None:
        text = " ".join(str(transcript or "").split())
        if not text or not self.cartesia_tts or not self.state or self.stopping:
            return
        if self.caller_speaking or self.phase == CallPhase.USER_SPEAKING:
            log.info("cartesia_tts_skipped", response_id=response_id, reason="caller_speaking")
            return
        playback_id = response_id or self.current_response_id or "cartesia-response"
        self.cartesia_tts_active.add(playback_id)
        playback_generation = self.response_manager.begin_playback(playback_id)
        self._set_assistant_speaking(True, "cartesia_tts_started")
        self._safe_transition(CallPhase.ASSISTANT_SPEAKING, "cartesia_tts_started")
        started = monotonic()
        try:
            ulaw_audio = await self.cartesia_tts.synthesize_ulaw(text, call_sid=self.state.call_sid)
        except Exception as exc:
            self.cartesia_tts_active.discard(playback_id)
            self._set_assistant_speaking(False, "cartesia_tts_failed")
            log.exception("cartesia_tts_playback_failed", response_id=response_id, error=str(exc))
            self.metrics.inc("cartesia_tts_failure_total")
            return
        if not self.response_manager.is_current(playback_generation, playback_id):
            self.cartesia_tts_active.discard(playback_id)
            self._set_assistant_speaking(False, "cartesia_stale_generation")
            log.info("cartesia_tts_discarded", response_id=response_id, reason="stale_generation")
            return
        self.metrics.inc("cartesia_tts_success_total")
        log.info(
            "cartesia_tts_latency",
            response_id=response_id,
            latency_ms=self._elapsed_ms(started, monotonic()),
            audio_bytes=len(ulaw_audio),
        )
        try:
            sent_count = await self._send_twilio_ulaw_audio(ulaw_audio, response_id=playback_id)
        except Exception as exc:
            self._set_assistant_speaking(False, "cartesia_audio_send_failed")
            log.exception("cartesia_audio_send_failed", response_id=playback_id, error=str(exc))
            self.metrics.inc("cartesia_tts_failure_total")
            return
        finally:
            self.cartesia_tts_active.discard(playback_id)
        if sent_count:
            self.cartesia_playback_started.add(playback_id)
            self.openai_audio_fallback_parts.pop(playback_id, None)
        else:
            log.warning("cartesia_audio_not_sent", response_id=playback_id, caller_speaking=self.caller_speaking, phase=self.phase.value)
            if not self.pending_marks:
                self._set_assistant_speaking(False, "cartesia_audio_not_sent")

    async def _send_twilio_ulaw_audio(self, audio: bytes, *, response_id: str) -> int:
        if not audio or not self.state or not self.state.stream_sid:
            return 0
        if self.caller_speaking or self.phase == CallPhase.USER_SPEAKING:
            log.info("cartesia_audio_skipped", response_id=response_id, reason="caller_speaking")
            return 0
        sent_count = 0
        chunk_size = 160
        for index in range(0, len(audio), chunk_size):
            if self.caller_speaking or self.phase == CallPhase.USER_SPEAKING or self.stopping:
                log.info(
                    "cartesia_audio_enqueue_stopped",
                    response_id=response_id,
                    reason="caller_speaking_or_stopping",
                    chunks_sent=sent_count,
                )
                break
            chunk = audio[index : index + chunk_size]
            payload = base64.b64encode(chunk).decode("ascii")
            if sent_count == 0:
                self._set_assistant_speaking(True, "cartesia_first_audio_delta")
                self._safe_transition(CallPhase.ASSISTANT_SPEAKING, "cartesia_first_audio_delta")
                self.response_first_audio_at = monotonic()
                if self.first_audio_delta_at is None:
                    self.first_audio_delta_at = self.response_first_audio_at
                log.info(
                    "assistant_audio_first_delta",
                    response_id=response_id,
                    source="cartesia",
                    bytes=len(chunk),
                    response_create_to_first_audio_ms=self._elapsed_ms(self.response_create_sent_at, self.response_first_audio_at),
                    call_to_first_audio_ms=self._elapsed_ms(self.call_started_at, self.response_first_audio_at),
                )
                log.info(
                    "cartesia_first_audio",
                    response_id=response_id,
                    first_audio_latency_ms=self._elapsed_ms(self.response_create_sent_at, self.response_first_audio_at),
                )
            if self._enqueue_twilio_message({"event": "media", "streamSid": self.state.stream_sid, "media": {"payload": payload}}, "audio"):
                sent_count += 1
        if sent_count == 0:
            return 0
        self.twilio_media_sent_count += sent_count
        self.response_audio_sent_counts[response_id] = self.response_audio_sent_counts.get(response_id, 0) + sent_count
        self.last_assistant_audio_at = monotonic()
        for _ in range(sent_count):
            self.metrics.inc("voice_twilio_media_sent_total")
        log.info(
            "twilio_audio_sent",
            response_id=response_id,
            source="cartesia",
            chunks=sent_count,
            audio_bytes=len(audio),
            chunk_size=chunk_size,
        )
        await self._send_twilio_mark(response_id)
        return sent_count

    async def _play_openai_audio_fallback(self, response_id: str | None) -> None:
        if self._using_cartesia_tts():
            log.error(
                "openai_audio_fallback_disabled_for_cartesia",
                response_id=response_id or self.current_response_id or "active",
            )
            if not self.pending_marks:
                self._set_assistant_speaking(False, "openai_audio_fallback_disabled")
            return
        fallback_id = response_id or self.current_response_id or "active"
        parts = self.openai_audio_fallback_parts.pop(fallback_id, [])
        if not parts:
            log.error("openai_audio_fallback_missing", response_id=fallback_id)
            if not self.pending_marks:
                self._set_assistant_speaking(False, "openai_audio_fallback_missing")
            return
        log.warning("openai_audio_fallback_playing", response_id=fallback_id, chunks=len(parts))
        sent_count = 0
        for payload in parts:
            sent_count += await self._send_twilio_audio(payload, response_id=fallback_id)
        if sent_count:
            await self._send_twilio_mark(fallback_id)
            return
        log.error("openai_audio_fallback_not_sent", response_id=fallback_id, chunks=len(parts))
        if not self.pending_marks:
            self._set_assistant_speaking(False, "openai_audio_fallback_not_sent")

    async def _delayed_cartesia_fallback(self, response_id: str) -> None:
        if self._using_cartesia_tts():
            log.error("cartesia_openai_audio_fallback_disabled", response_id=response_id)
            return
        await asyncio.sleep(0.4)
        if (
            response_id in self.cartesia_playback_started
            or response_id in self.cartesia_tts_active
            or response_id not in self.openai_audio_fallback_parts
        ):
            return
        log.warning("cartesia_transcript_missing_using_openai_fallback", response_id=response_id)
        await self._play_openai_audio_fallback(response_id)

    async def _send_twilio_mark(self, response_id: str) -> None:
        if not self.state or not self.state.stream_sid:
            return
        mark_name = f"openai-{response_id}"[:120]
        if not self._enqueue_twilio_message({"event": "mark", "streamSid": self.state.stream_sid, "mark": {"name": mark_name}}, "mark"):
            log.warning("twilio_mark_enqueue_failed", mark_name=mark_name)
            return
        self.pending_marks.add(mark_name)
        self.mark_response_ids[mark_name] = response_id
        log.info("twilio_mark_sent", mark_name=mark_name, pending_marks=len(self.pending_marks))

    async def _clear_twilio_buffer(self) -> None:
        if self.state and self.state.stream_sid:
            self._enqueue_twilio_message({"event": "clear", "streamSid": self.state.stream_sid}, "clear")
            self.metrics.inc("voice_twilio_clear_total")
            log.info("twilio_clear_sent")

    async def _send_openai(self, event: dict) -> None:
        if not self.openai_ws:
            raise RuntimeError("OpenAI realtime websocket is not connected")
        await self.openai_ws.send(json.dumps(event))

    async def _send_session_update(self, session_instructions: str, *, reason: str) -> None:
        cartesia_mode = self._using_cartesia_tts()
        output_modalities = ["text"] if cartesia_mode else ["audio"]
        audio_config: dict[str, Any] = {
            "input": {
                "format": {"type": "audio/pcmu"},
                "transcription": {"model": self.settings.openai_transcription_model},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.80,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 800,
                    "idle_timeout_ms": 6000,
                    "interrupt_response": False,
                    "create_response": False,
                },
            },
        }
        if not cartesia_mode:
            audio_config["output"] = {
                "format": {"type": "audio/pcmu"},
                "voice": self.active_realtime_voice,
            }
        log.info(
            "session_update_config",
            reason=reason,
            model=self.settings.openai_realtime_model,
            voice=None if cartesia_mode else self.active_realtime_voice,
            speed=None if cartesia_mode else self.settings.openai_realtime_speed,
            input_format="audio/pcmu",
            output_format="text_only_cartesia" if cartesia_mode else "audio/pcmu",
            output_modalities=output_modalities,
            transcription_model=self.settings.openai_transcription_model,
            cartesia_enabled=cartesia_mode,
        )
        await self._send_openai(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": session_instructions,
                    "output_modalities": output_modalities,
                    "audio": audio_config,
                },
            }
        )

    async def _maybe_fallback_realtime_voice(self, event: dict) -> None:
        if self._using_cartesia_tts():
            return
        if self.voice_fallback_applied or self.active_realtime_voice == "coral":
            return
        error_text = json.dumps(event.get("error") or event, default=str).lower()
        voice_related = any(token in error_text for token in ("voice", "marin", "unsupported", "invalid", "session"))
        if not voice_related:
            return
        previous_voice = self.active_realtime_voice
        self.active_realtime_voice = "coral"
        self.voice_fallback_applied = True
        log.warning(
            "openai_realtime_voice_fallback",
            previous_voice=previous_voice,
            fallback_voice=self.active_realtime_voice,
            reason="openai_error",
            error_preview=error_text[:300],
        )
        try:
            await self._send_session_update(
                _realtime_instructions(self.customer_memory.get("language", "hinglish")),
                reason="voice_fallback",
            )
        except Exception as exc:
            log.exception(
                "openai_realtime_voice_fallback_failed",
                previous_voice=previous_voice,
                fallback_voice=self.active_realtime_voice,
                error=str(exc),
            )

    async def _request_assistant_response(self, instructions: str, *, reason: str, force: bool = False) -> bool:
        allowed, blocked_reason = self._response_gate(reason=reason, force=force)
        log.info(
            "response_gate_check",
            reason=reason,
            force=force,
            allowed=allowed,
            blocked_reason=blocked_reason,
            response_active=self.assistant_response_active,
            assistant_speaking=self.assistant_speaking,
            pending_marks=len(self.pending_marks),
            greeting_locked=self._greeting_locked(),
        )
        if not allowed:
            if reason == "user_transcript":
                self.deferred_response_instructions = instructions
                self.deferred_response_reason = reason
            log.info(
                "response_gate_blocked",
                reason=reason,
                blocked_reason=blocked_reason,
                deferred=reason == "user_transcript",
            )
            log.info(
                "response_create_suppressed",
                reason=reason,
                response_active=self.assistant_response_active,
                assistant_speaking=self.assistant_speaking,
                greeting_locked=self._greeting_locked(),
            )
            return False
        log.info("response_gate_passed", reason=reason)
        log.info("response_create_instructions_preview", reason=reason, chars=len(instructions), preview=_preview(instructions))
        self.pending_response_create_reason = reason
        await self._send_openai({"type": "response.create", "response": {"instructions": instructions}})
        self.response_create_sent_at = monotonic()
        if reason == "greeting":
            self.greeting_response_create_at = self.response_create_sent_at
        log.info(
            "response_create_sent",
            reason=reason,
            session_ack_to_response_create_ms=self._elapsed_ms(self.session_update_ack_at, self.response_create_sent_at),
            session_update_sent_to_response_create_ms=self._elapsed_ms(self.session_update_sent_at, self.response_create_sent_at),
            ws_to_response_create_ms=self._elapsed_ms(self.openai_ws_connected_at, self.response_create_sent_at),
            call_to_response_create_ms=self._elapsed_ms(self.call_started_at, self.response_create_sent_at),
        )
        return True

    @staticmethod
    def _elapsed_ms(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return round((end - start) * 1000, 2)

    def _response_gate(self, *, reason: str, force: bool = False) -> tuple[bool, str | None]:
        if force:
            return True, None
        if self._greeting_locked():
            return False, "greeting_locked"
        if reason == "user_transcript" and (self.caller_speaking or self.phase == CallPhase.USER_SPEAKING):
            return False, "caller_speaking"
        if self.assistant_response_active:
            return False, "assistant_response_active"
        if self.assistant_speaking:
            if not self.pending_marks and self.response_completed_at:
                self._set_assistant_speaking(False, "stale_speaking_state_recovered")
                return True, None
            return False, "assistant_speaking"
        return True, None

    async def _flush_deferred_response(self) -> None:
        if not self.deferred_response_instructions or self.stopping:
            return
        instructions = self.deferred_response_instructions
        reason = self.deferred_response_reason or "deferred_user_transcript"
        self.deferred_response_instructions = None
        self.deferred_response_reason = None
        log.info("deferred_response_flush", reason=reason)
        await self._request_assistant_response(instructions, reason=reason)

    async def _handle_user_transcript(self, transcript: str | None) -> None:
        async with self._transcript_lock:
            await self._handle_user_transcript_locked(transcript)

    async def _handle_user_transcript_locked(self, transcript: str | None) -> None:
        text = str(transcript or "")
        if not self.state:
            return
        if self._greeting_locked():
            log.info("user_transcript_ignored_during_greeting", chars=len(text))
            return
        if not self._add_transcript_turn("user", transcript):
            return
        self.silence_prompt_count = 0
        self._transition(CallPhase.PROCESSING_USER, "user_transcript_completed")
        updates = extract_customer_facts(text, self.customer_memory)
        updates.update(detect_language_update(text, self.customer_memory))
        profile_result = classify_customer_profile_with_confidence(text, self.customer_memory)
        profile = profile_result.get("profile")
        if profile:
            updates["customer_profile"] = profile
            updates["intent_type"] = profile
            log.info(
                "intent_classification_result",
                customer_profile=profile,
                confidence=profile_result.get("confidence"),
                reason=profile_result.get("reason"),
            )
        objections = detect_objections(text)
        if objections:
            updates["objections"] = merge_objections(self.customer_memory, objections)
        updates["conversation_metrics"] = user_turn_metrics(self.customer_memory, text, objections)
        stage_result = determine_stage_with_reason(text, self.customer_memory, objections)
        stage = stage_result.get("stage")
        if stage:
            updates["conversation_stage"] = stage
            log.info(
                "conversation_stage_result",
                stage=stage,
                confidence=stage_result.get("confidence"),
                reason=stage_result.get("reason"),
            )
        projected_memory = dict(self.customer_memory)
        projected_memory.update({key: value for key, value in updates.items() if key != "objections"})
        if objections:
            projected_memory["objections"] = updates["objections"]
        updates["lead_score"] = calculate_lead_score(projected_memory)
        if updates:
            self.customer_memory = await self.call_memory.update_memory(self.state.call_sid, updates)
            log.info("customer_memory_updated", fields=sorted(updates.keys()))
            if "language" in updates:
                log.info("language_locked", language=self.customer_memory.get("language"), locked=self.customer_memory.get("language_locked"))
            if "conversation_stage" in updates:
                log.info("conversation_stage_changed", stage=self.customer_memory.get("conversation_stage"))
            if "customer_profile" in updates:
                log.info("intent_classified", customer_profile=self.customer_memory.get("customer_profile"))
            if objections:
                log.info("objection_detected", objections=objections)
        else:
            self.customer_memory = await self.call_memory.load_memory(self.state.call_sid)
        if not self.customer_memory.get("intro_delivered") and self._is_twilio_trial_notice(text):
            log.info("pre_intro_transcript_ignored", reason="twilio_trial_notice")
            return
        forced_response = self._forced_safety_response(text)
        if forced_response:
            if not self.customer_memory.get("intro_delivered"):
                self.customer_memory = await self.call_memory.update_memory(
                    self.state.call_sid,
                    {
                        "intro_delivered": True,
                        "conversation_stage": "INTRO",
                    },
                )
            if self._using_cartesia_tts():
                await self._speak_fixed_text(
                    forced_response,
                    response_id="cartesia-forced-safety",
                    reason="forced_safety_response",
                )
                return
            await self._request_assistant_response(
                "Say exactly this one line, then stop. Do not add anything else:\n"
                f"{forced_response}",
                reason="forced_safety_response",
                force=True,
            )
            return
        if not self.customer_memory.get("intro_delivered"):
            # If no customer_name, greeting was already the intro (direct intro line).
            # Skip OUTGOING_INTRO_LINE to avoid speaking intro twice.
            customer_name = (
                str(self.customer_memory.get("customer_name") or self.customer_memory.get("name") or "").strip()
                or getattr(self.claims, "customer_name", None)
            )
            self.customer_memory = await self.call_memory.update_memory(
                self.state.call_sid,
                {
                    "intro_delivered": True,
                    "conversation_stage": "INTRO",
                },
            )
            self._drop_last_short_user_turn(text, reason="intro_confirmation")
            if customer_name:
                # Greeting was a confirm-name line — now speak the actual intro
                if self._using_cartesia_tts():
                    await self._speak_fixed_text(
                        OUTGOING_INTRO_LINE,
                        response_id="cartesia-outgoing-intro",
                        reason="outgoing_intro",
                    )
                    return
                await self._request_assistant_response(
                    "Say exactly this one line, then stop. Do not add anything else:\n"
                    f"{OUTGOING_INTRO_LINE}",
                    reason="outgoing_intro",
                    force=True,
                )
            # No customer_name: greeting was already the intro — skip to discovery
            return
        await self._request_assistant_response(
            self._build_response_instructions(),
            reason="user_transcript",
        )

    async def _record_assistant_question(self, transcript: str | None) -> None:
        if not self.state:
            return
        field = infer_asked_field(str(transcript or ""))
        if not field:
            return
        updates = record_question(self.customer_memory, field)
        self.customer_memory = await self.call_memory.update_memory(self.state.call_sid, updates)
        log.info("asked_question_recorded", field=field)

    async def _record_assistant_metrics(self, transcript: str | None) -> None:
        if not self.state:
            return
        latency_ms = self._elapsed_ms(self.response_started_at, monotonic())
        metrics = assistant_turn_metrics(self.customer_memory, str(transcript or ""), response_latency_ms=latency_ms)
        self.customer_memory = await self.call_memory.update_memory(self.state.call_sid, {"conversation_metrics": metrics})
        if metrics.get("robotic_behavior_detected"):
            log.warning("robotic_behavior_detected", metrics=metrics)

    def _build_response_instructions(self) -> str:
        stage = self.customer_memory.get("conversation_stage", "INTRO")
        language = self.customer_memory.get("language", "hinglish")

        if self.customer_memory.get("callback_requested"):
            callback_time = self.customer_memory.get("callback_time") or "unspecified time"
            return (
                SYSTEM_PROMPT
                + "\n\n"
                + build_dynamic_response_context(
                    get_persona_context(language),
                    build_memory_context(self.customer_memory),
                    language_context=get_language_context(self.customer_memory),
                )
                + f"\n\nCaller is busy. Confirm callback at {callback_time}.\n"
                "Reply in one short line, then close."
            )

        do_not_ask = ", ".join(
            field
            for field in ("budget", "bhk", "location_interest", "purpose", "visit_interest")
            if not can_ask(self.customer_memory, field)
        )

        return (
            SYSTEM_PROMPT
            + "\n\n"
            + build_dynamic_response_context(
                get_persona_context(language),
                build_memory_context(self.customer_memory),
                stage_context=get_stage_context(self.customer_memory) if stage in ("SITE_VISIT_BOOKING", "CLOSING") else "",
                profile_context=get_profile_context(self.customer_memory),
                lead_score_context=get_lead_score_context(self.customer_memory),
                objection_context=get_objection_context(self.customer_memory) if self.customer_memory.get("objections") else "",
                language_context=get_language_context(self.customer_memory),
                matched_project_context=project_context(self.customer_memory),
            )
            + f"\n\nDo not ask: {do_not_ask or 'none'}.\n"
            "Reply now. One sentence only."
        )

    def _forced_safety_response(self, text: str) -> str | None:
        lowered = str(text or "").lower()
        if any(term in lowered for term in CALL_REASON_TERMS):
            log.info("call_reason_forced_response", transcript_preview=_preview(text, 180))
            return CALL_REASON_RESPONSE
        if any(term in lowered for term in UNSUPPORTED_INVENTORY_TERMS):
            log.warning("unsupported_inventory_forced_response", transcript_preview=_preview(text, 180))
            return UNSUPPORTED_AREA_RESPONSE
        if any(term.lower() in lowered for term in AI_IDENTITY_TERMS):
            language = self.customer_memory.get("language") or "hinglish"
            log.warning("ai_identity_forced_response", language=language, transcript_preview=_preview(text, 180))
            if language == "english":
                return AI_IDENTITY_RESPONSE_ENGLISH
            return AI_IDENTITY_RESPONSE_HINDI
        return None

    @staticmethod
    def _is_twilio_trial_notice(text: str) -> bool:
        lowered = str(text or "").lower()
        return "trial account" in lowered or "remove this message" in lowered

    def _greeting_text(self) -> str:
        customer_name = (
            str(self.customer_memory.get("customer_name") or self.customer_memory.get("name") or "").strip()
            or getattr(self.claims, "customer_name", None)
        )
        if not customer_name:
            return "Haan ji, main Riya bol rahi hoon DreamHome se. Property enquiry ke baare mein call kiya tha."
        return OUTGOING_CONFIRM_LINE.format(customer_name=customer_name)

    def _greeting_has_customer_name(self) -> bool:
        return bool(
            str(self.customer_memory.get("customer_name") or self.customer_memory.get("name") or "").strip()
            or getattr(self.claims, "customer_name", None)
        )

    def _build_greeting_instructions(self) -> str:
        confirm_line = self._greeting_text()
        instructions = (
            "Say exactly this one line in a warm, natural Indian phone-call tone, then stop "
            "and wait for the caller to respond. Do not add anything else:\n"
            f"{confirm_line}"
        )
        log.info(
            "greeting_instructions_preview",
            greeting_text=confirm_line,
            chars=len(instructions),
            preview=_preview(instructions),
        )
        return instructions

    def _handle_response_started(self, event: dict) -> None:
        response_id = (event.get("response") or {}).get("id") or event.get("response_id")
        reason = self.pending_response_create_reason
        self.pending_response_create_reason = None
        if self.assistant_response_active and response_id != self.current_response_id:
            log.info("response_started_while_active", current_response_id=self.current_response_id, new_response_id=response_id)
        self.assistant_response_active = True
        self.current_response_id = response_id
        self.response_manager.begin_response(response_id)
        self.response_started_at = monotonic()
        self.response_completed_at = None
        self.response_first_audio_at = None
        if reason == "greeting":
            self.greeting_response_id = response_id
            self.greeting_manager.start(response_id or "openai-greeting")
            self.greeting_clear_locked_until = float("inf")
        log.info("assistant_response_started", response_id=response_id, reason=reason)
        self._spawn(self._response_audio_watchdog(response_id), "response_audio_watchdog")

    def _handle_response_completed(self, event: dict) -> None:
        response_id = (event.get("response") or {}).get("id") or self.current_response_id
        self.response_completed_at = monotonic()
        self.assistant_response_active = False
        self.response_manager.begin_response(None)
        if response_id == self.current_response_id:
            self.current_response_id = None
        response_ms = self._elapsed_ms(self.response_started_at, self.response_completed_at)
        if response_ms is not None:
            self.metrics.observe_ms("assistant_response", response_ms)
        log.info("assistant_response_completed", response_id=response_id)
        if self.close_after_current_response and not self.assistant_speaking and not self.pending_marks:
            self._spawn(self._stop_after_response(), "close_after_response")

    async def _handle_twilio_mark(self, message: dict) -> None:
        mark_name = (message.get("mark") or {}).get("name")
        if mark_name:
            self.pending_marks.discard(mark_name)
            response_id = self.mark_response_ids.pop(mark_name, None)
            if not self.pending_marks:
                self._set_assistant_speaking(False, "twilio_mark_ack")
                if self.phase == CallPhase.USER_SPEAKING or self.caller_speaking:
                    log.info(
                        "twilio_mark_ack_state_transition_skipped",
                        mark_name=mark_name,
                        current_state=self.phase,
                        caller_speaking=self.caller_speaking,
                        reason="caller_speaking",
                    )
                elif not self.assistant_response_active:
                    self._safe_transition(CallPhase.WAITING_FOR_USER, "twilio_mark_ack")
            if response_id == self.greeting_response_id and not self.greeting_completed:
                self.greeting_completed = True
                self.greeting_manager.complete(response_id)
                self.greeting_clear_locked_until = 0.0
                log.info(
                    "greeting_completed",
                    response_id=response_id,
                    greeting_latency_ms=self._elapsed_ms(self.call_started_at, self.greeting_manager.started_at),
                    greeting_playback_ms=self._elapsed_ms(self.greeting_manager.started_at, self.greeting_manager.completed_at),
                )
            if response_id:
                log.info("cartesia_playback_completed", response_id=response_id, pending_marks=len(self.pending_marks))
            log.info("twilio_mark_ack", mark_name=mark_name, pending_marks=len(self.pending_marks), assistant_speaking=self.assistant_speaking)
            if (
                not self.pending_marks
                and not self.assistant_response_active
                and self.phase != CallPhase.USER_SPEAKING
                and not self.caller_speaking
            ):
                if self.close_after_current_response:
                    await self._stop()
                    return
                await self._flush_deferred_response()

    async def _confirm_barge_in_after_delay(self) -> None:
        await asyncio.sleep(BARGE_IN_DEBOUNCE_SECONDS)
        if self.stopping or not self.caller_speaking or not self.caller_speech_started_at:
            return
        speech_seconds = monotonic() - self.caller_speech_started_at
        if speech_seconds < BARGE_IN_DEBOUNCE_SECONDS:
            return
        if self._greeting_locked():
            log.info("interruption_ignored_during_greeting", speech_ms=round(speech_seconds * 1000, 2))
            return
        if not (self.assistant_speaking or self.assistant_response_active):
            log.info("barge_in_ignored_no_assistant_audio", speech_ms=round(speech_seconds * 1000, 2))
            return
        log.info("barge_in_confirmed", speech_ms=round(speech_seconds * 1000, 2), response_id=self.current_response_id)
        log.info("user_interruption", speech_ms=round(speech_seconds * 1000, 2), response_id=self.current_response_id)
        await self._clear_twilio_buffer()
        await self._cancel_current_openai_response("barge_in")

    async def _cancel_current_openai_response(self, reason: str) -> None:
        if (
            not self.assistant_response_active
            and not self.assistant_speaking
            and self.current_response_id is None
            and not getattr(self, "pending_marks", set())
            and not getattr(self, "cartesia_tts_active", set())
        ):
            log.info("assistant_response_cancel_ignored", reason=reason)
            return
        if self.openai_ws and self.assistant_response_active:
            with contextlib.suppress(Exception):
                await self._send_openai({"type": "response.cancel"})
        self.assistant_response_active = False
        self._set_assistant_speaking(False, reason)
        self.current_response_id = None
        if hasattr(self, "response_manager"):
            self.response_manager.cancel(reason)
        cancelled_marks = len(getattr(self, "pending_marks", set()))
        cancelled_tts = len(getattr(self, "cartesia_tts_active", set()))
        if hasattr(self, "pending_marks"):
            self.pending_marks.clear()
        if hasattr(self, "mark_response_ids"):
            self.mark_response_ids.clear()
        if hasattr(self, "cartesia_tts_active"):
            self.cartesia_tts_active.clear()
        if hasattr(self, "openai_audio_fallback_parts"):
            self.openai_audio_fallback_parts.clear()
        self.response_completed_at = monotonic()
        log.info(
            "assistant_response_cancelled",
            reason=reason,
            cancelled_marks=cancelled_marks,
            cancelled_tts=cancelled_tts,
        )

    async def _twilio_sender_loop(self) -> None:
        while not self.stopping:
            raw = await self.twilio_outbound_queue.get()
            try:
                await self.websocket.send_text(raw)
            finally:
                self.twilio_outbound_queue.task_done()

    def _enqueue_twilio_message(self, message: dict[str, Any], kind: str) -> bool:
        try:
            self.twilio_outbound_queue.put_nowait(json.dumps(message))
        except asyncio.QueueFull:
            self.metrics.inc("voice_twilio_outbound_queue_full_total")
            log.error("twilio_outbound_queue_full", kind=kind, queue_depth=self.twilio_outbound_queue.qsize())
            return False
        return True

    async def _openai_heartbeat_loop(self) -> None:
        while not self.stopping and self.openai_ws:
            await asyncio.sleep(OPENAI_HEARTBEAT_SECONDS)
            if self.stopping or not self.openai_ws:
                return
            started = monotonic()
            try:
                pong = await self.openai_ws.ping()
                await asyncio.wait_for(pong, timeout=OPENAI_PONG_TIMEOUT_SECONDS)
                log.info("openai_heartbeat_ok", latency_ms=self._elapsed_ms(started, monotonic()))
            except asyncio.TimeoutError:
                self.metrics.inc("websocket_ping_timeout_total")
                log.warning("openai_heartbeat_timeout", timeout_seconds=OPENAI_PONG_TIMEOUT_SECONDS, phase=self.phase.value)
                await self._recover_openai_connection("heartbeat_timeout")
                return
            except Exception as exc:
                log.warning("openai_heartbeat_failed", error=str(exc), phase=self.phase.value)
                await self._recover_openai_connection(exc.__class__.__name__)
                return

    async def _recover_openai_connection(self, reason: str) -> None:
        if self.stopping or self.openai_recovering:
            return
        self.openai_recovering = True
        self.openai_ready = False
        old_ws = self.openai_ws
        self.openai_ws = None
        with contextlib.suppress(Exception):
            if old_ws:
                await old_ws.close()
        safe_reconnect_phases = {CallPhase.SESSION_READY, CallPhase.WAITING_FOR_USER, CallPhase.USER_SPEAKING, CallPhase.PROCESSING_USER}
        try:
            if self.phase not in safe_reconnect_phases or self.assistant_speaking or self.assistant_response_active:
                await self._stop(failure_reason=f"openai realtime closed during unsafe phase: {reason}")
                return
            if self.openai_reconnect_attempts >= OPENAI_RECONNECT_ATTEMPTS:
                await self._stop(failure_reason=f"openai realtime closed: {reason}")
                return
            self.openai_reconnect_attempts += 1
            self.metrics.inc("openai_reconnect_total")
            log.info("openai_reconnect_started", reason=reason, attempt=self.openai_reconnect_attempts, phase=self.phase.value)
            await asyncio.sleep(min(4.0, 2 ** (self.openai_reconnect_attempts - 1)))
            await self._connect_openai(request_greeting=False)
        finally:
            self.openai_recovering = False

    def _set_assistant_speaking(self, value: bool, reason: str) -> None:
        if self.assistant_speaking == value:
            return
        self.assistant_speaking = value
        log.info("assistant_state_changed", field="assistant_speaking", value=value, reason=reason)

    async def _response_audio_watchdog(self, response_id: str | None) -> None:
        timeout_seconds = CARTESIA_RESPONSE_TIMEOUT_SECONDS if self._using_cartesia_tts() else RESPONSE_AUDIO_TIMEOUT_SECONDS
        await asyncio.sleep(timeout_seconds)
        if self.stopping or response_id != self.current_response_id or self.response_first_audio_at:
            return
        if self._using_cartesia_tts() and self.cartesia_tts_active:
            return
        log.warning("fallback_triggered", reason="response_audio_timeout", response_id=response_id, timeout_seconds=timeout_seconds)
        await self._cancel_current_openai_response("response_audio_timeout")
        if self._using_cartesia_tts():
            await self._speak_fixed_text("Ji sir, ek second.", response_id="cartesia-response-timeout", reason="response_audio_timeout")
            return
        await self._request_assistant_response("Ji sir, ek second.", reason="response_audio_timeout", force=True)

    async def _silence_watchdog(self) -> None:
        while not self.stopping:
            await asyncio.sleep(2.0)
            if self.call_started_at and monotonic() - self.call_started_at >= MAX_CALL_SECONDS:
                log.info("call_max_duration_reached", max_call_seconds=MAX_CALL_SECONDS)
                self.close_after_current_response = True
                await self._request_assistant_response("Sir, main details WhatsApp pe bhej deti hoon. Namaste ji.", reason="max_duration_close")
                continue
            if self.assistant_response_active or self.assistant_speaking or self.caller_speaking or self._greeting_locked():
                continue
            idle_seconds = monotonic() - self.last_activity_at
            if idle_seconds >= SILENCE_PROMPT_SECONDS:
                self.last_activity_at = monotonic()
                self.silence_prompt_count += 1
                log.info(
                    "fallback_triggered",
                    reason="silence_watchdog",
                    idle_seconds=round(idle_seconds, 2),
                    silence_prompt_count=self.silence_prompt_count,
                )
                if self.silence_prompt_count >= MAX_SILENCE_PROMPTS:
                    self.close_after_current_response = True
                    await self._request_assistant_response("Sir, main details WhatsApp pe bhej deti hoon. Namaste ji.", reason="silence_close")
                else:
                    await self._request_assistant_response("Hello sir?", reason="silence_watchdog")

    async def _stop_after_response(self) -> None:
        await asyncio.sleep(0.8)
        await self._stop()

    def _greeting_locked(self) -> bool:
        manager = getattr(self, "greeting_manager", None)
        manager_playing = bool(manager and manager.is_playing)
        return manager_playing or (
            not getattr(self, "greeting_completed", False)
            and monotonic() < getattr(self, "greeting_clear_locked_until", 0.0)
        )

    async def _stop(self, failure_reason: str | None = None) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._transition(CallPhase.FAILED if failure_reason else CallPhase.CALL_ENDING, failure_reason or "normal_stop")
        await self._close_openai()
        if self.cartesia_tts:
            with contextlib.suppress(Exception):
                await self.cartesia_tts.close()
        if self.state:
            summary = await self._summarize_and_persist_call()
            async with SessionLocal() as session:
                repo = CallRepository(session)
                call = await repo.get_by_sid(self.state.call_sid)
                if call:
                    await repo.finish_call(call, summary, failure_reason)
            await self.memory.delete(self.state.call_sid)
        self._transition(CallPhase.FAILED if failure_reason else CallPhase.COMPLETED, failure_reason or "call_completed")
        self.metrics.inc("call_failure_total" if failure_reason else "call_success_total")
        log.info("call_finished", failure_reason=failure_reason)
        with contextlib.suppress(Exception):
            await self.websocket.close()

    async def _summarize_and_persist_call(self) -> dict[str, Any] | None:
        if not self.state:
            return None
        transcript = self._full_transcript()
        log.info("call_completion_started", transcript_chars=len(transcript), transcript_turns=len(self.transcript_turns))
        summary = await self.summarizer.summarize(transcript)
        payload = self._crm_payload(summary, transcript)
        await self.crm_outbox.enqueue_and_try_delivery(payload)
        log.info("call_completion_saved", lead_status=summary.get("lead_status"))
        return summary

    def _add_transcript_turn(self, role: str, text: str | None) -> bool:
        cleaned = " ".join(str(text or "").split())
        if not cleaned or cleaned in {".", ",", "?", "!", "h", "hm", "haan?"}:
            log.info("transcript_ignored", role=role, text=cleaned)
            return False
        self.transcript_turns.append({"role": role, "text": cleaned})
        log.info("transcript_accumulated", role=role, chars=len(cleaned), turns=len(self.transcript_turns))
        return True

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
            "lead_score": int(self.customer_memory.get("lead_score") or 0),
            "language": _text_or_none(self.customer_memory.get("language")),
            "conversation_stage": _text_or_none(self.customer_memory.get("conversation_stage")),
            "customer_profile": _text_or_none(self.customer_memory.get("customer_profile")),
            "visit_day": _text_or_none(self.customer_memory.get("visit_day")),
            "visit_time": _text_or_none(self.customer_memory.get("visit_time")),
            "decision_maker": _text_or_none(self.customer_memory.get("decision_maker")),
            "enriched_memory": self.customer_memory,
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
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if not exc:
            return
        log.exception("background_task_failed", task_name=task.get_name(), error=str(exc))
        self.metrics.inc("voice_background_task_failed_total")
        if not self.stopping and task.get_name() in CRITICAL_BACKGROUND_TASKS:
            asyncio.create_task(self._stop(failure_reason=f"background task failed: {task.get_name()}"))

    def _transition(self, phase: CallPhase, reason: str) -> None:
        if self.phase == phase:
            return
        previous = self.phase
        if phase not in ALLOWED_PHASE_TRANSITIONS.get(previous, set()):
            self.metrics.inc("voice_invalid_state_transition_total")
            log.error("invalid_call_state_transition", previous=previous.value, attempted=phase.value, reason=reason)
            raise RuntimeError(f"invalid call state transition: {previous.value} -> {phase.value}")
        self.phase = phase
        self.metrics.inc("voice_state_transition_total")
        log.info("call_state_transition", previous=previous.value, new=phase.value, reason=reason)

    def _safe_transition(self, phase: CallPhase, reason: str) -> bool:
        try:
            self._transition(phase, reason)
            return True
        except RuntimeError as exc:
            log.warning(
                "call_state_transition_skipped",
                current=self.phase.value,
                attempted=phase.value,
                reason=reason,
                error=str(exc),
            )
            return False

    async def _cancel_tasks(self) -> None:
        tasks = [task for task in self.tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self.settings.provider_cleanup_timeout_seconds)


def _realtime_instructions(language: str = "hinglish") -> str:
    return (
        SYSTEM_PROMPT
        + "\n\n"
        + get_persona_context(language)
        + "\n\nCall rules:\n"
        "- Greeting already sent. Never repeat it.\n"
        "- React pehle, phir respond. Short rakhna.\n"
        "- Silence se mat ghabrao. Pressure mat daalo.\n"
        "- Current language mein raho unless caller explicitly switches.\n"
        "- Off-topic sawaal par same language mein short redirect.\n"
        "- If caller busy, ask callback time and close warmly.\n"
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


def _preview(text: str, limit: int = 600) -> str:
    return " ".join(str(text or "").split())[:limit]
