import json
from typing import Any

import httpx

from app.config import Settings
from app.prompts.real_estate_agent import SUMMARY_PROMPT
from app.utils.logging import log


LEAD_INFO_FIELDS = {
    "name",
    "pg_for",
    "sharing_preference",
    "budget",
    "move_in_date",
    "occupation",
    "whatsapp_confirmation",
    "visit_interest",
    "objections",
}
VALID_LEAD_STATUSES = {"new", "qualified", "visit_booked", "callback_scheduled", "not_interested", "needs_follow_up"}


class CallSummarizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def summarize(self, transcript: str) -> dict[str, Any]:
        if not transcript.strip():
            return _fallback_summary("No transcript captured.")

        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.settings.openai_summary_model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SUMMARY_PROMPT},
                            {"role": "user", "content": f"Transcript:\n{transcript}"},
                        ],
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("summary_generation_failed", error=_safe_error(exc))
            return _fallback_summary("Summary generation failed.", transcript)

        try:
            parsed = json.loads(content)
        except Exception as exc:
            log.warning("summary_json_invalid", error=_safe_error(exc), raw_preview=str(content)[:300])
            return _fallback_summary("Summary JSON was invalid.", transcript)

        summary = _normalize_summary(parsed)
        log.info("summary_generated", lead_status=summary["lead_status"], chars=len(summary["summary"]))
        return summary


def _normalize_summary(value: dict[str, Any]) -> dict[str, Any]:
    lead_info = value.get("lead_info")
    if not isinstance(lead_info, dict):
        lead_info = {}
    normalized_lead_info = {field: lead_info.get(field) for field in LEAD_INFO_FIELDS}

    lead_status = str(value.get("lead_status") or "needs_follow_up").strip()
    if lead_status not in VALID_LEAD_STATUSES:
        lead_status = "needs_follow_up"

    return {
        "summary": str(value.get("summary") or "No summary available.").strip(),
        "lead_status": lead_status,
        "sentiment": _optional_text(value.get("sentiment")),
        "outcome": _optional_text(value.get("outcome")),
        "lead_info": normalized_lead_info,
    }


def _fallback_summary(reason: str, transcript: str | None = None) -> dict[str, Any]:
    preview = f" Transcript preview: {transcript[:220]}" if transcript else ""
    return {
        "summary": f"{reason}{preview}".strip(),
        "lead_status": "needs_follow_up",
        "sentiment": None,
        "outcome": "needs manual review",
        "lead_info": {field: None for field in LEAD_INFO_FIELDS},
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:240] or exc.__class__.__name__
