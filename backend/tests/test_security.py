import os
import asyncio

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
from app.services.stt import DeepgramSTTStream
from app.telephony.twilio import inbound_stream_twiml
from app.telephony.stream_auth import StreamTokenError
from app.utils.text import sanitize_tts


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
