import asyncio
import base64
import json
from time import monotonic
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings
from app.conversation.customer_profile import classify_customer_profile_with_confidence
from app.conversation.fact_extractor import extract_customer_facts
from app.conversation.language_manager import detect_language_update
from app.conversation.lead_scoring import calculate_lead_score
from app.conversation.objection_detector import detect_objections, merge_objections
from app.conversation.quality_metrics import user_turn_metrics
from app.conversation.stage_manager import determine_stage_with_reason
from app.database.session import SessionLocal
from app.services.call_repository import CallRepository
from app.services.call_summary import CallSummarizer
from app.services.cartesia_streaming_tts import CartesiaStreamingTTS
from app.services.crm_outbox import CRMOutbox
from app.services.memory import ConversationMemory, ConversationState
from app.services.openai_text_llm import OpenAITextLLM
from app.services.sentence_stream import sentence_fragments
from app.services.stt import DeepgramSTTStream
from app.services.supabase_crm import SupabaseCRM
from app.telephony.stream_auth import StreamClaims, StreamTokenService
from app.utils.logging import call_sid_ctx, log
from app.websocket.twilio_media import CallPhase, TwilioMediaSession


class TwilioTextStreamingSession(TwilioMediaSession):
    def __init__(
        self,
        websocket: WebSocket,
        settings: Settings,
        memory: ConversationMemory,
        stream_tokens: StreamTokenService,
        claims: StreamClaims,
        metrics,
        summarizer: CallSummarizer,
        crm: SupabaseCRM,
        crm_outbox: CRMOutbox,
    ):
        super().__init__(websocket, settings, memory, stream_tokens, claims, metrics, summarizer, crm, crm_outbox)
        self.stt: DeepgramSTTStream | None = None
        self.text_llm = OpenAITextLLM(settings)
        self.streaming_tts = CartesiaStreamingTTS(settings)
        self.active_llm_task: asyncio.Task | None = None
        self.active_tts_context_id: str | None = None
        self.current_user_speech_started_at: float | None = None
        self.current_user_final_at: float | None = None
        self.current_llm_started_at: float | None = None
        self.current_llm_first_token_at: float | None = None
        self.current_tts_started_at: float | None = None
        self.current_tts_first_audio_at: float | None = None

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
            log.exception("twilio_text_streaming_session_failed", error=str(exc))
            await self._stop(failure_reason=str(exc))
        finally:
            await self._cancel_tasks()
            await self._close_text_pipeline()
            self.metrics.inc("voice_ws_disconnected_total")

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

        log.info(
            "call_started",
            stream_sid=stream_sid,
            media_format=media_format,
            active_voice_pipeline="text_streaming",
            active_stt_provider="deepgram",
            active_llm_provider="openai_text",
            active_tts_provider="cartesia_streaming",
            cartesia_model=self.settings.cartesia_model_id,
            cartesia_voice_id_suffix=self.settings.cartesia_voice_id[-6:] if self.settings.cartesia_voice_id else None,
            cartesia_sample_rate=self.settings.cartesia_sample_rate,
            cartesia_encoding=self.settings.cartesia_encoding,
            deepgram_model=self.settings.deepgram_model,
            deepgram_language=self.settings.deepgram_language,
        )
        self.stt = DeepgramSTTStream(self.settings, self._on_deepgram_transcript, self._on_deepgram_speech_started)
        await self.stt.connect()
        self._safe_transition(CallPhase.SESSION_READY, "text_streaming_ready")
        self._spawn(self._speak_text(self._greeting_text(), reason="greeting"), "text_streaming_greeting")
        self._spawn(self._silence_watchdog(), "silence_watchdog")

    async def _media(self, message: dict) -> None:
        self.last_activity_at = monotonic()
        payload = (message.get("media") or {}).get("payload")
        if not payload or not self.stt:
            return
        try:
            audio = base64.b64decode(payload, validate=True)
        except Exception:
            self.metrics.inc("voice_bad_media_payload_total")
            return
        await self.stt.send_audio(audio)
        self.metrics.inc("voice_twilio_media_frames_total")

    async def _on_deepgram_speech_started(self) -> None:
        self.last_activity_at = monotonic()
        log.info("barge_in_detected", assistant_speaking=self.assistant_speaking)
        self.caller_speaking = True
        self.caller_speech_started_at = monotonic()
        self.current_user_speech_started_at = self.caller_speech_started_at
        self._safe_transition(CallPhase.USER_SPEAKING, "deepgram_speech_started")
        await self._cancel_active_generation("barge_in")
        await self._clear_twilio_buffer()

    async def _on_deepgram_transcript(self, transcript: str, is_final: bool, confidence: float | None) -> None:
        if not transcript.strip():
            return
        log.info(
            "deepgram_transcript_received",
            is_final=is_final,
            confidence=confidence,
            chars=len(transcript),
        )
        if not is_final:
            return
        self.caller_speaking = False
        self.caller_speech_started_at = None
        self.current_user_final_at = monotonic()
        log.info(
            "stt_final_latency",
            stt_latency_ms=self._elapsed_ms(self.current_user_speech_started_at, self.current_user_final_at),
            transcript_chars=len(transcript),
        )
        await self._handle_user_transcript(transcript)

    async def _handle_user_transcript_locked(self, transcript: str | None) -> None:
        if not self.state or not self._add_transcript_turn("user", transcript):
            return
        self.silence_prompt_count = 0
        self._safe_transition(CallPhase.PROCESSING_USER, "user_transcript_completed")
        text = str(transcript or "")
        updates = extract_customer_facts(text, self.customer_memory)
        updates.update(detect_language_update(text, self.customer_memory))
        profile_result = classify_customer_profile_with_confidence(text, self.customer_memory)
        profile = profile_result.get("profile")
        if profile:
            updates["customer_profile"] = profile
            updates["intent_type"] = profile
        objections = detect_objections(text)
        if objections:
            updates["objections"] = merge_objections(self.customer_memory, objections)
        updates["conversation_metrics"] = user_turn_metrics(self.customer_memory, text, objections)
        stage_result = determine_stage_with_reason(text, self.customer_memory, objections)
        stage = stage_result.get("stage")
        if stage:
            updates["conversation_stage"] = stage
        projected_memory = dict(self.customer_memory)
        projected_memory.update({key: value for key, value in updates.items() if key != "objections"})
        if objections:
            projected_memory["objections"] = updates["objections"]
        updates["lead_score"] = calculate_lead_score(projected_memory)
        self.customer_memory = await self.call_memory.update_memory(self.state.call_sid, updates)
        log.info("customer_memory_updated", fields=sorted(updates.keys()))

        if not self.customer_memory.get("intro_delivered") and self._is_twilio_trial_notice(text):
            log.info("pre_intro_transcript_ignored", reason="twilio_trial_notice")
            return

        forced_response = self._forced_safety_response(text)
        if forced_response:
            await self._speak_text(forced_response, reason="forced_safety_response")
            return

        if not self.customer_memory.get("intro_delivered"):
            self.customer_memory = await self.call_memory.update_memory(
                self.state.call_sid,
                {"intro_delivered": True, "conversation_stage": "INTRO"},
            )
            await self._generate_text_response(
                self._build_response_instructions()
                + "\n\nCaller has replied to the opening. Follow Step 2 intro naturally. Do not start discovery.",
                reason="outgoing_intro",
            )
            return

        await self._generate_text_response(self._build_response_instructions(), reason="user_transcript")

    async def _generate_text_response(self, instructions: str, *, reason: str) -> None:
        await self._cancel_active_generation("new_response")
        self.active_llm_task = self._spawn(self._run_text_generation(instructions, reason=reason), "openai_text_generation")

    async def _run_text_generation(self, instructions: str, *, reason: str) -> None:
        if not self.state:
            return
        self.current_llm_started_at = monotonic()
        self.current_llm_first_token_at = None
        log.info("llm_stream_started", reason=reason, model=self.settings.openai_text_model)

        async def deltas():
            async for delta in self.text_llm.stream_response(instructions=instructions, turns=self.transcript_turns):
                if self.current_llm_first_token_at is None:
                    self.current_llm_first_token_at = monotonic()
                    log.info(
                        "llm_first_token",
                        reason=reason,
                        latency_ms=self._elapsed_ms(self.current_llm_started_at, self.current_llm_first_token_at),
                        stt_to_llm_first_token_ms=self._elapsed_ms(self.current_user_final_at, self.current_llm_first_token_at),
                    )
                yield delta

        full_response = ""
        async for sentence in sentence_fragments(
            deltas(),
            min_chars=self.settings.min_tts_fragment_chars,
            max_chars=self.settings.max_tts_fragment_chars,
        ):
            full_response = f"{full_response} {sentence}".strip()
            log.info("sentence_chunk_ready", reason=reason, chars=len(sentence), preview=sentence[:80])
            await self._speak_text(sentence, reason="llm_sentence")
        if full_response:
            self._add_transcript_turn("assistant", full_response)
        log.info("llm_stream_finished", reason=reason, chars=len(full_response))

    async def _speak_text(self, text: str, *, reason: str) -> None:
        if not self.state or not text.strip() or self.stopping:
            return
        response_id = f"text-{int(monotonic() * 1000)}"
        self.current_tts_started_at = monotonic()
        self.current_tts_first_audio_at = None
        self._set_assistant_speaking(True, f"cartesia_stream_started:{reason}")
        self._safe_transition(CallPhase.ASSISTANT_SPEAKING, f"cartesia_stream_started:{reason}")
        log.info("cartesia_stream_started", reason=reason, response_id=response_id, chars=len(text))
        sent_count = 0
        try:
            async for context_id, payload in self.streaming_tts.stream_ulaw_payloads(text, call_sid=self.state.call_sid):
                self.active_tts_context_id = context_id
                if self.caller_speaking or self.phase == CallPhase.USER_SPEAKING:
                    log.info("cartesia_stream_audio_skipped", reason="caller_speaking", response_id=response_id)
                    break
                if self.current_tts_first_audio_at is None:
                    self.current_tts_first_audio_at = monotonic()
                    log.info(
                        "cartesia_first_audio",
                        response_id=response_id,
                        tts_first_audio_ms=self._elapsed_ms(self.current_tts_started_at, self.current_tts_first_audio_at),
                        stt_to_first_audio_ms=self._elapsed_ms(self.current_user_final_at, self.current_tts_first_audio_at),
                        llm_first_token_to_first_audio_ms=self._elapsed_ms(self.current_llm_first_token_at, self.current_tts_first_audio_at),
                        total_mouth_to_ear_ms=self._elapsed_ms(self.current_user_speech_started_at, self.current_tts_first_audio_at),
                    )
                if self._enqueue_twilio_message({"event": "media", "streamSid": self.state.stream_sid, "media": {"payload": payload}}, "audio"):
                    sent_count += 1
                    self.twilio_media_sent_count += 1
            if sent_count:
                log.info("twilio_audio_sent", response_id=response_id, chunks=sent_count)
        except Exception as exc:
            log.exception(
                "cartesia_stream_failed",
                reason=reason,
                response_id=response_id,
                error=str(exc),
                openai_voice_fallback=False,
            )
        finally:
            self.active_tts_context_id = None
        if sent_count:
            await self._send_twilio_mark(response_id)
        else:
            self._set_assistant_speaking(False, "cartesia_stream_no_audio")
        log.info("cartesia_stream_finished", reason=reason, response_id=response_id, chunks=sent_count)

    async def _cancel_active_generation(self, reason: str) -> None:
        if self.active_llm_task and not self.active_llm_task.done():
            self.active_llm_task.cancel()
            log.info("openai_text_stream_cancelled", reason=reason)
        if self.active_tts_context_id:
            await self.streaming_tts.cancel(self.active_tts_context_id)
            self.active_tts_context_id = None
        self._set_assistant_speaking(False, reason)

    async def _close_text_pipeline(self) -> None:
        if self.stt:
            await self.stt.close()
        await self.streaming_tts.close()
        await self.text_llm.close()
