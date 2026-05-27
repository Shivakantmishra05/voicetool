from collections import defaultdict
from time import monotonic

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config import Settings
from app.utils.logging import new_request_id, request_id_ctx


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.limit = settings.rate_limit_per_minute
        self.window = 60.0
        self.clients: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith("/health") or request.url.path.startswith("/ready"):
            return await call_next(request)
        now = monotonic()
        client = request.client.host if request.client else "unknown"
        hits = [ts for ts in self.clients[client] if now - ts < self.window]
        hits.append(now)
        self.clients[client] = hits
        if len(hits) > self.limit:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        return await call_next(request)

