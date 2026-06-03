import asyncio
from typing import Any

from supabase import Client, create_client

from app.config import Settings
from app.utils.logging import log


class SupabaseCRM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Client | None = None
        if settings.supabase_url and settings.supabase_key:
            self.client = create_client(str(settings.supabase_url), settings.supabase_key)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def insert_call(self, payload: dict[str, Any]) -> bool:
        if not self.client:
            log.info("supabase_insert_skipped", reason="SUPABASE_URL or SUPABASE_KEY not configured", call_sid=payload.get("call_sid"))
            return False

        log.info("crm_insert_attempt", call_sid=payload.get("call_sid"), payload_keys=len(payload), payload_size_bytes=len(str(payload)))
        try:
            response = await _with_retries(lambda: self.client.table("calls").upsert(payload, on_conflict="call_sid").execute())
            inserted = response.data or []
            log.info("crm_insert_success", call_sid=payload.get("call_sid"), rows=len(inserted))
            log.info("supabase_insert_success", call_sid=payload.get("call_sid"), rows=len(inserted))
            return True
        except Exception as exc:
            error = _safe_error(exc)
            if _looks_like_schema_mismatch(error):
                legacy_payload = _legacy_payload(payload)
                try:
                    response = await _with_retries(lambda: self.client.table("calls").upsert(legacy_payload, on_conflict="call_sid").execute())
                    inserted = response.data or []
                    log.warning(
                        "supabase_insert_success_legacy_schema",
                        call_sid=payload.get("call_sid"),
                        rows=len(inserted),
                        dropped_fields=sorted(set(payload) - set(legacy_payload)),
                    )
                    return True
                except Exception as retry_exc:
                    log.exception("crm_insert_failure", call_sid=payload.get("call_sid"), error=_safe_error(retry_exc), legacy=True)
                    log.exception("supabase_insert_failure", call_sid=payload.get("call_sid"), error=_safe_error(retry_exc))
                    return False
            log.exception("crm_insert_failure", call_sid=payload.get("call_sid"), error=error, legacy=False)
            log.exception("supabase_insert_failure", call_sid=payload.get("call_sid"), error=error)
            return False

    async def list_leads(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.client:
            return []
        try:
            response = await asyncio.to_thread(
                lambda: self.client.table("calls").select("*").order("created_at", desc=True).limit(limit).execute()
            )
            return response.data or []
        except Exception as exc:
            log.warning("supabase_list_leads_failed", error=_safe_error(exc))
            return []

    async def get_lead(self, call_sid: str) -> dict[str, Any] | None:
        if not self.client:
            return None
        try:
            response = await asyncio.to_thread(
                lambda: self.client.table("calls").select("*").eq("call_sid", call_sid).limit(1).execute()
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception as exc:
            log.warning("supabase_get_lead_failed", call_sid=call_sid, error=_safe_error(exc))
            return None


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:240] or exc.__class__.__name__


async def _with_retries(operation, attempts: int = 3):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(operation)
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            log.warning("supabase_retrying", attempt=attempt, error=_safe_error(exc))
            await asyncio.sleep(0.4 * attempt)
    raise last_exc or RuntimeError("supabase operation failed")


def _looks_like_schema_mismatch(error: str) -> bool:
    lowered = error.lower()
    return "column" in lowered or "schema cache" in lowered or "could not find" in lowered


def _legacy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "call_sid",
        "phone_number",
        "caller_name",
        "pg_for",
        "sharing_preference",
        "budget",
        "move_in_date",
        "occupation",
        "whatsapp_confirmation",
        "visit_interest",
        "lead_status",
        "sentiment",
        "outcome",
        "objections",
        "summary",
        "full_transcript",
    }
    return {key: value for key, value in payload.items() if key in allowed}
