import asyncio
from dataclasses import asdict, dataclass
from time import perf_counter

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.rest import Client as TwilioClient

from app.config import Settings
from app.services.memory import ConversationMemory
from app.utils.logging import log


@dataclass
class ProviderCheck:
    name: str
    status: str
    latency_ms: float | None = None
    detail: str | None = None


@dataclass
class StartupDiagnostics:
    environment: str
    checks: list[ProviderCheck]

    @property
    def ready(self) -> bool:
        return all(check.status == "ok" for check in self.checks)

    def public_dict(self) -> dict:
        return {"environment": self.environment, "ready": self.ready, "checks": [asdict(check) for check in self.checks]}


async def run_startup_diagnostics(settings: Settings, memory: ConversationMemory, session: AsyncSession) -> StartupDiagnostics:
    checks = [
        await _check_database(session, settings),
        await _check_redis(memory, settings),
    ]
    if settings.startup_provider_checks_enabled:
        provider_checks = await asyncio.gather(
            _check_twilio(settings),
            _check_openai(settings),
            _check_supabase(settings),
        )
        checks.extend(provider_checks)
    diagnostics = StartupDiagnostics(environment=settings.environment, checks=checks)
    for check in diagnostics.checks:
        log.info("startup_provider_check", provider=check.name, status=check.status, latency_ms=check.latency_ms, detail=check.detail)
    if settings.startup_provider_checks_required and not diagnostics.ready:
        failed = ", ".join(check.name for check in diagnostics.checks if check.status != "ok")
        raise RuntimeError(f"Startup diagnostics failed: {failed}")
    return diagnostics


async def _check_database(session: AsyncSession, settings: Settings) -> ProviderCheck:
    return await _timed("postgresql" if str(settings.database_url).startswith("postgresql") else "database", lambda: session.execute(text("select 1")))


async def _check_redis(memory: ConversationMemory, settings: Settings) -> ProviderCheck:
    if not settings.redis_url:
        return ProviderCheck("redis", "skipped", detail="REDIS_URL not configured")
    if not memory.redis:
        return ProviderCheck("redis", "degraded", detail="using local memory fallback")
    return await _timed("redis", memory.redis.ping)


async def _check_twilio(settings: Settings) -> ProviderCheck:
    async def run():
        client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        return await asyncio.to_thread(client.api.accounts(settings.twilio_account_sid).fetch)

    return await _timed("twilio", run, settings.provider_check_timeout_seconds)


async def _check_openai(settings: Settings) -> ProviderCheck:
    model = settings.openai_text_model if settings.voice_pipeline == "text_streaming" else settings.openai_realtime_model

    async def run():
        async with httpx.AsyncClient(timeout=settings.provider_check_timeout_seconds) as client:
            response = await client.get(
                f"https://api.openai.com/v1/models/{model}",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            response.raise_for_status()

    return await _timed("openai", run, settings.provider_check_timeout_seconds)


async def _check_supabase(settings: Settings) -> ProviderCheck:
    if not settings.supabase_url or not settings.supabase_key:
        return ProviderCheck("supabase", "skipped", detail="SUPABASE_URL or SUPABASE_KEY not configured")

    async def run():
        async with httpx.AsyncClient(timeout=settings.provider_check_timeout_seconds) as client:
            response = await client.get(
                f"{str(settings.supabase_url).rstrip('/')}/rest/v1/calls?select=id&limit=1",
                headers={"apikey": settings.supabase_key, "Authorization": f"Bearer {settings.supabase_key}"},
            )
            response.raise_for_status()

    return await _timed("supabase", run, settings.provider_check_timeout_seconds)


async def _timed(name: str, operation, timeout: float | None = None) -> ProviderCheck:
    start = perf_counter()
    try:
        if timeout:
            await asyncio.wait_for(operation(), timeout=timeout)
        else:
            await operation()
        return ProviderCheck(name, "ok", round((perf_counter() - start) * 1000, 2))
    except Exception as exc:
        return ProviderCheck(name, "failed", round((perf_counter() - start) * 1000, 2), _safe_detail(exc))


def _safe_detail(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text[:180] or exc.__class__.__name__
