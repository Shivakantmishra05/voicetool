import os
import asyncio
from time import monotonic
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DEEPGRAM_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("ELEVENLABS_VOICE_ID", "test")
os.environ.setdefault("STREAM_TOKEN_SECRET", "test-stream-secret")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-admin-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("STARTUP_PROVIDER_CHECKS_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.config import Settings
from app.conversation.fact_extractor import extract_customer_facts
from app.conversation.memory_manager import CallMemoryManager
from app.conversation.stage_manager import determine_stage_with_reason
from app.services.stt import DeepgramSTTStream
from app.telephony.twilio import inbound_stream_twiml
from app.telephony.stream_auth import StreamTokenError
from app.utils.text import sanitize_tts
from app.observability.metrics import Metrics
from app.websocket.twilio_media import ALLOWED_PHASE_TRANSITIONS, CallPhase, TwilioMediaSession


def test_dashboard_requires_auth():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 401


def test_stream_token_replay_is_rejected():
    with TestClient(app):
        async def run():
            token = app.state.stream_tokens.issue("CA123")
            claims = await app.state.stream_tokens.validate_for_handshake(token)
            await app.state.stream_tokens.consume_for_start(claims, "CA123")
            try:
                await app.state.stream_tokens.validate_for_handshake(token)
            except StreamTokenError:
                return
            raise AssertionError("replayed stream token was accepted")

        asyncio.run(run())


def test_twiml_uses_path_token_not_query_token():
    with TestClient(app):
        token = app.state.stream_tokens.issue("CA123")
        twiml = inbound_stream_twiml(app.state.settings, "+15551234567", token)
    assert f"/ws/twilio/{token}" in twiml
    assert "?token=" not in twiml
    assert "<Say" not in twiml


def test_leads_requires_auth():
    with TestClient(app) as client:
        response = client.get("/leads")
    assert response.status_code == 401


def test_deepgram_list_payload_does_not_crash_parser():
    transcripts = []
    speech_started = {"value": False}

    async def on_transcript(text, is_final, confidence):
        transcripts.append((text, is_final, confidence))

    async def on_speech_started():
        speech_started["value"] = True

    async def run():
        stt = DeepgramSTTStream(Settings(), on_transcript, on_speech_started)
        await stt._handle_raw_message(
            '[{"type":"SpeechStarted"},{"type":"Results","is_final":true,"channel":{"alternatives":[{"transcript":"hello","confidence":0.91}]}}]'
        )

    asyncio.run(run())

    assert speech_started["value"] is True
    assert transcripts == [("hello", True, 0.91)]


def test_sanitize_tts_removes_markup_and_limits_words():
    text = "# Hello  **sir**\n" + " ".join(f"word{i}" for i in range(40))
    sanitized = sanitize_tts(text)

    assert "#" not in sanitized
    assert "*" not in sanitized
    assert "\n" not in sanitized
    assert len(sanitized.split()) == 35


def test_invalid_voice_state_regression_is_not_allowed():
    assert CallPhase.OPENAI_CONNECTING not in ALLOWED_PHASE_TRANSITIONS[CallPhase.ASSISTANT_SPEAKING]


def test_late_final_transcript_can_process_from_waiting_for_user():
    assert CallPhase.PROCESSING_USER in ALLOWED_PHASE_TRANSITIONS[CallPhase.WAITING_FOR_USER]


def test_late_twilio_mark_ack_does_not_override_user_speaking_state():
    session = object.__new__(TwilioMediaSession)
    session.pending_marks = {"openai-old-response"}
    session.mark_response_ids = {"openai-old-response": "resp_old"}
    session.greeting_response_id = None
    session.greeting_completed = False
    session.assistant_speaking = True
    session.assistant_response_active = False
    session.phase = CallPhase.USER_SPEAKING
    session.caller_speaking = True
    session.deferred_response_instructions = "ignored while user is speaking"
    session.deferred_response_reason = "user_transcript"
    session.stopping = False
    session.metrics = Metrics()

    asyncio.run(session._handle_twilio_mark({"mark": {"name": "openai-old-response"}}))

    assert session.phase == CallPhase.USER_SPEAKING
    assert session.assistant_speaking is False
    assert session.pending_marks == set()
    assert session.deferred_response_instructions == "ignored while user is speaking"


def test_barge_in_cancel_is_idempotent_when_response_already_cancelled():
    session = object.__new__(TwilioMediaSession)
    session.openai_ws = None
    session.assistant_response_active = False
    session.assistant_speaking = False
    session.current_response_id = None
    session.response_completed_at = None

    asyncio.run(session._cancel_current_openai_response("barge_in"))

    assert session.assistant_response_active is False
    assert session.assistant_speaking is False
    assert session.current_response_id is None
    assert session.response_completed_at is None


def test_greeting_lock_ignores_speech_started_without_changing_state():
    session = object.__new__(TwilioMediaSession)
    session.last_activity_at = 0.0
    session.greeting_completed = False
    session.greeting_clear_locked_until = monotonic() + 5
    session.caller_speaking = False
    session.caller_speech_started_at = None
    session.phase = CallPhase.ASSISTANT_SPEAKING

    asyncio.run(session._handle_openai_event({"type": "input_audio_buffer.speech_started"}))

    assert session.phase == CallPhase.ASSISTANT_SPEAKING
    assert session.caller_speaking is False
    assert session.caller_speech_started_at is None


def test_failed_twilio_mark_enqueue_does_not_create_pending_mark():
    session = object.__new__(TwilioMediaSession)
    session.state = SimpleNamespace(stream_sid="MZ123")
    session.pending_marks = set()
    session.mark_response_ids = {}
    session._enqueue_twilio_message = lambda *_: False

    asyncio.run(session._send_twilio_mark("resp_123"))

    assert session.pending_marks == set()
    assert session.mark_response_ids == {}


def test_audio_delta_is_skipped_while_caller_is_speaking():
    session = object.__new__(TwilioMediaSession)
    session.state = SimpleNamespace(stream_sid="MZ123")
    session.caller_speaking = True
    session.phase = CallPhase.USER_SPEAKING
    session.metrics = Metrics()
    enqueued = {"value": False}

    def enqueue(*_):
        enqueued["value"] = True
        return True

    session._enqueue_twilio_message = enqueue

    asyncio.run(session._send_twilio_audio("payload", response_id="resp_old"))

    assert enqueued["value"] is False
    assert session.metrics.counters["voice_stale_audio_skipped_total"] == 1


def test_user_transcript_response_is_deferred_while_caller_speaking():
    session = object.__new__(TwilioMediaSession)
    session.greeting_completed = True
    session.greeting_clear_locked_until = 0.0
    session.assistant_response_active = False
    session.assistant_speaking = False
    session.pending_marks = set()
    session.response_completed_at = None
    session.caller_speaking = True
    session.phase = CallPhase.USER_SPEAKING

    allowed, reason = session._response_gate(reason="user_transcript")

    assert allowed is False
    assert reason == "caller_speaking"


def test_audio_done_without_sent_audio_does_not_enqueue_mark():
    session = object.__new__(TwilioMediaSession)
    session.response_audio_sent_counts = {}
    session.twilio_media_sent_count = 0
    session.current_response_id = "resp_empty"
    mark_sent = {"value": False}

    async def send_mark(_response_id):
        mark_sent["value"] = True

    session._send_twilio_mark = send_mark

    asyncio.run(session._handle_openai_event({"type": "response.audio.done", "response_id": "resp_empty"}))

    assert mark_sent["value"] is False


def test_language_switch_overwrites_default_hinglish_memory():
    async def run():
        manager = CallMemoryManager()
        return await manager.update_memory("CA123", {"language": "english", "language_locked": True})

    memory = asyncio.run(run())

    assert memory["language"] == "english"
    assert memory["language_locked"] is True


def test_fact_extractor_does_not_treat_bhk_or_sector_as_budget():
    assert extract_customer_facts("Sector 150 me 2 bedroom flat dekh raha hu").get("budget") is None
    assert extract_customer_facts("Budget around 80 lakh hai")["budget"] == "80 lakh"


def test_short_transcript_keeps_existing_conversation_stage():
    result = determine_stage_with_reason("haan", {"conversation_stage": "RECOMMENDATION"}, [])

    assert result["stage"] == "RECOMMENDATION"
    assert result["reason"] == "short_transcript_keep_current_stage"
