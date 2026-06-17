import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, health, leads, twilio
from app.config import get_settings
from app.database.init_db import init_db
from app.middleware.request_context import InMemoryRateLimitMiddleware, RequestContextMiddleware
from app.observability import Metrics
from app.database.session import SessionLocal
from app.services.memory import ConversationMemory
from app.services.call_summary import CallSummarizer
from app.services.crm_outbox import CRMOutbox
from app.services.startup_diagnostics import run_startup_diagnostics
from app.services.supabase_crm import SupabaseCRM
from app.telephony.stream_auth import StreamTokenService
from app.telephony.stream_auth import StreamTokenError
from app.utils.logging import configure_logging
from app.utils.logging import log
from app.websocket.twilio_media import TwilioMediaSession

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_startup()
    log.info(
        "openai_realtime_config",
        model=settings.openai_realtime_model,
        voice=settings.openai_realtime_voice,
        speed=settings.openai_realtime_speed,
        tts_provider=settings.tts_provider,
        cartesia_model=settings.cartesia_model_id if settings.tts_provider == "cartesia" else None,
        cartesia_voice_id_suffix=settings.cartesia_voice_id[-6:] if settings.tts_provider == "cartesia" and settings.cartesia_voice_id else None,
    )
    await init_db()
    memory = ConversationMemory(settings)
    await memory.connect()
    app.state.settings = settings
    app.state.metrics = Metrics()
    app.state.memory = memory
    app.state.stream_tokens = StreamTokenService(settings, memory)
    app.state.summarizer = CallSummarizer(settings)
    app.state.crm = SupabaseCRM(settings)
    app.state.crm_outbox = CRMOutbox(app.state.crm, app.state.metrics)
    app.state.crm_outbox_task = None
    async with SessionLocal() as session:
        app.state.startup_diagnostics = await run_startup_diagnostics(settings, memory, session)
    app.state.crm_outbox_task = asyncio.create_task(app.state.crm_outbox.run_forever(), name="crm_outbox_worker")
    yield
    app.state.crm_outbox.stop()
    if app.state.crm_outbox_task:
        app.state.crm_outbox_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.crm_outbox_task
    await app.state.memory.close()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(InMemoryRateLimitMiddleware, settings=settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(health.router)
app.include_router(twilio.router)
app.include_router(dashboard.router)
app.include_router(leads.router)


async def _twilio_media_authenticated(websocket: WebSocket, stream_token: str | None):
    query_token = websocket.query_params.get("token")
    token = stream_token or query_token
    token_transport = "path" if stream_token else "query" if query_token else "missing"
    query_keys = sorted(websocket.query_params.keys())
    try:
        log.info(
            "twilio_ws_auth_attempt",
            token_transport=token_transport,
            query_keys=query_keys,
            token_preview=app.state.stream_tokens.preview(token),
            client=str(websocket.client),
        )
        claims = await app.state.stream_tokens.validate_for_handshake(token)
        log.info(
            "twilio_ws_auth_ok",
            token_transport=token_transport,
            call_sid=claims.call_sid,
            nonce_suffix=claims.nonce[-6:],
            exp=claims.exp,
        )
    except StreamTokenError as exc:
        log.warning(
            "twilio_ws_auth_rejected",
            reason=str(exc),
            token_transport=token_transport,
            query_keys=query_keys,
            token_preview=app.state.stream_tokens.preview(token),
            client=str(websocket.client),
        )
        await websocket.close(code=1008)
        return
    session = TwilioMediaSession(
        websocket,
        settings,
        app.state.memory,
        app.state.stream_tokens,
        claims,
        app.state.metrics,
        app.state.summarizer,
        app.state.crm,
        app.state.crm_outbox,
    )
    await session.run()


@app.websocket("/ws/twilio/{stream_token}")
async def twilio_media_path_token(websocket: WebSocket, stream_token: str):
    await _twilio_media_authenticated(websocket, stream_token)


@app.websocket("/ws/twilio")
async def twilio_media_query_token(websocket: WebSocket):
    await _twilio_media_authenticated(websocket, None)
