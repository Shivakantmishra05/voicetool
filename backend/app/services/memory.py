import json
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis

from app.config import Settings
from app.utils.logging import log


@dataclass
class ConversationState:
    call_sid: str
    stream_sid: str | None = None
    caller_number: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    lead_info: dict[str, Any] = field(default_factory=dict)
    is_ai_speaking: bool = False
    turn_sequence: int = 0


class ConversationMemory:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._redis: redis.Redis | None = None
        self._local: dict[str, ConversationState] = {}
        self._used_stream_nonces: dict[str, float] = {}

    async def connect(self) -> None:
        if not self.settings.redis_url:
            return
        try:
            self._redis = redis.from_url(str(self.settings.redis_url), decode_responses=True)
            await self._redis.ping()
        except Exception as exc:
            self._redis = None
            if self.settings.require_redis:
                raise
            log.warning("redis_unavailable_using_memory_fallback", error=str(exc))

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    @property
    def redis(self) -> redis.Redis | None:
        return self._redis

    async def get(self, call_sid: str) -> ConversationState | None:
        if self._redis:
            raw = await self._redis.get(self._key(call_sid))
            if raw:
                return ConversationState(**json.loads(raw))
        return self._local.get(call_sid)

    async def set(self, state: ConversationState) -> None:
        state.turns = _trim_turns(state.turns, self.settings.max_memory_turns, self.settings.max_transcript_chars)
        if self._redis:
            await self._redis.set(self._key(state.call_sid), json.dumps(state.__dict__), ex=86400)
        self._local[state.call_sid] = state

    async def delete(self, call_sid: str) -> None:
        if self._redis:
            await self._redis.delete(self._key(call_sid))
        self._local.pop(call_sid, None)

    @staticmethod
    def _key(call_sid: str) -> str:
        return f"call:{call_sid}:state"

    async def is_stream_nonce_used(self, nonce: str) -> bool:
        if self._redis:
            return bool(await self._redis.exists(self._nonce_key(nonce)))
        self._prune_local_nonces()
        return nonce in self._used_stream_nonces

    async def consume_stream_nonce(self, nonce: str, ttl_seconds: int) -> bool:
        if self._redis:
            return bool(await self._redis.set(self._nonce_key(nonce), "1", ex=ttl_seconds, nx=True))
        self._prune_local_nonces()
        if nonce in self._used_stream_nonces:
            return False
        self._used_stream_nonces[nonce] = __import__("time").time() + ttl_seconds
        return True

    @staticmethod
    def _nonce_key(nonce: str) -> str:
        return f"stream_nonce:{nonce}"

    def _prune_local_nonces(self) -> None:
        now = __import__("time").time()
        self._used_stream_nonces = {nonce: exp for nonce, exp in self._used_stream_nonces.items() if exp > now}


def _trim_turns(turns: list[dict[str, Any]], max_turns: int, max_chars: int) -> list[dict[str, Any]]:
    trimmed = turns[-max_turns:]
    total = 0
    kept: list[dict[str, Any]] = []
    for turn in reversed(trimmed):
        total += len(str(turn.get("text", "")))
        if total > max_chars:
            break
        kept.append(turn)
    return list(reversed(kept))
