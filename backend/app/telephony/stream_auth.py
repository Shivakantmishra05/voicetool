import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from time import time

from app.config import Settings
from app.services.memory import ConversationMemory


@dataclass(frozen=True)
class StreamClaims:
    call_sid: str
    nonce: str
    exp: int


class StreamTokenError(ValueError):
    pass


class StreamTokenService:
    def __init__(self, settings: Settings, memory: ConversationMemory):
        self.settings = settings
        self.memory = memory

    def issue(self, call_sid: str) -> str:
        claims = {
            "call_sid": call_sid,
            "nonce": secrets.token_urlsafe(18),
            "exp": int(time() + self.settings.stream_token_ttl_seconds),
        }
        body = _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        sig = _sign(body, self.settings.resolved_stream_token_secret)
        return f"{body}.{sig}"

    def preview(self, token: str | None) -> dict:
        try:
            claims = self._decode(token)
            return {
                "call_sid": claims.call_sid,
                "nonce_suffix": claims.nonce[-6:],
                "exp": claims.exp,
            }
        except StreamTokenError as exc:
            return {"error": str(exc)}

    async def validate_for_handshake(self, token: str | None) -> StreamClaims:
        claims = self._decode(token)
        if claims.exp < int(time()):
            raise StreamTokenError("stream token expired")
        if await self.memory.is_stream_nonce_used(claims.nonce):
            raise StreamTokenError("stream token replayed")
        return claims

    async def consume_for_start(self, claims: StreamClaims, call_sid: str) -> None:
        if not hmac.compare_digest(claims.call_sid, call_sid):
            raise StreamTokenError("stream token call binding mismatch")
        if not await self.memory.consume_stream_nonce(claims.nonce, self.settings.stream_token_ttl_seconds):
            raise StreamTokenError("stream token replayed")

    def _decode(self, token: str | None) -> StreamClaims:
        if not token or "." not in token:
            raise StreamTokenError("missing stream token")
        body, sig = token.rsplit(".", 1)
        expected = _sign(body, self.settings.resolved_stream_token_secret)
        if not hmac.compare_digest(sig, expected):
            raise StreamTokenError("invalid stream token signature")
        try:
            payload = json.loads(_unb64(body))
            return StreamClaims(call_sid=str(payload["call_sid"]), nonce=str(payload["nonce"]), exp=int(payload["exp"]))
        except Exception as exc:
            raise StreamTokenError("invalid stream token payload") from exc


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)
