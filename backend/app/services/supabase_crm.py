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

    async def insert_call(self, payload: dict[str, Any]) -> None:
        if not self.client:
            log.info("supabase_insert_skipped", reason="SUPABASE_URL or SUPABASE_KEY not configured", call_sid=payload.get("call_sid"))
            return

        try:
            response = await asyncio.to_thread(lambda: self.client.table("calls").insert(payload).execute())
            inserted = response.data or []
            log.info("supabase_insert_success", call_sid=payload.get("call_sid"), rows=len(inserted))
        except Exception as exc:
            log.exception("supabase_insert_failure", call_sid=payload.get("call_sid"), error=_safe_error(exc))

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
