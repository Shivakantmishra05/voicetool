from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import NoDecode
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "DreamHome Voice Agent"
    environment: str = Field(default="local", pattern="^(local|staging|production|test)$")
    public_base_url: AnyHttpUrl = "https://example.ngrok-free.app"
    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:8000", "http://127.0.0.1:8000"]

    database_url: PostgresDsn | str = "sqlite+aiosqlite:///./dreamhome.db"
    redis_url: RedisDsn | str | None = "redis://redis:6379/0"
    require_redis: bool = False

    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str | None = None
    stream_token_secret: str | None = None
    stream_token_ttl_seconds: int = 120

    openai_api_key: str
    openai_realtime_model: str = "gpt-realtime"
    openai_summary_model: str = "gpt-4.1-mini"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"

    supabase_url: AnyHttpUrl | None = None
    supabase_key: str | None = None

    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en-IN"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash-lite"
    llm_timeout_seconds: float = 4.0

    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_fallback_voice_id: str | None = None
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_output_format: str = "pcm_16000"
    tts_timeout_seconds: float = 8.0

    rate_limit_per_minute: int = 120
    call_idle_timeout_seconds: int = 45
    max_turns_in_prompt: int = 16
    agent_response_max_chars: int = 280
    max_memory_turns: int = 40
    max_transcript_chars: int = 32000
    outbound_audio_queue_size: int = 64
    transcript_queue_size: int = 12
    llm_sentence_queue_size: int = 8
    provider_cleanup_timeout_seconds: float = 3.0
    deepgram_keepalive_seconds: float = 5.0
    deepgram_reconnect_attempts: int = 2
    websocket_max_message_chars: int = 12000
    startup_provider_checks_enabled: bool = True
    startup_provider_checks_required: bool = False
    provider_check_timeout_seconds: float = 5.0
    deepgram_endpointing_ms: int = 500
    deepgram_utterance_end_ms: int = 1500
    min_tts_fragment_chars: int = 18
    max_tts_fragment_chars: int = 80

    admin_username: str | None = None
    admin_password: str | None = None
    admin_session_secret: str | None = None
    admin_session_ttl_seconds: int = 43200

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                import json

                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON value must be a list")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def websocket_url(self) -> str:
        base = str(self.public_base_url).rstrip("/")
        return base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/twilio"

    def validate_startup(self) -> None:
        missing = []
        for name in (
            "twilio_account_sid",
            "twilio_auth_token",
            "openai_api_key",
        ):
            if not getattr(self, name, None):
                missing.append(name.upper())
        if self.environment == "production":
            for name in ("stream_token_secret", "admin_username", "admin_password", "admin_session_secret"):
                if not getattr(self, name, None):
                    missing.append(name.upper())
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if self.deepgram_utterance_end_ms < 1000:
            raise RuntimeError("DEEPGRAM_UTTERANCE_END_MS must be >= 1000 for Deepgram live streaming")
        if self.elevenlabs_output_format not in {"pcm_16000", "ulaw_8000"}:
            raise RuntimeError("ELEVENLABS_OUTPUT_FORMAT must be pcm_16000 or ulaw_8000 for Twilio playback")

    @property
    def resolved_stream_token_secret(self) -> str:
        return self.stream_token_secret or self.twilio_auth_token

    @property
    def resolved_admin_session_secret(self) -> str:
        return self.admin_session_secret or self.twilio_auth_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
