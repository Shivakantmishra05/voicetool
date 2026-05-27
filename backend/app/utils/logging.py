import logging
import sys
from contextvars import ContextVar
from uuid import uuid4

import structlog

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
call_sid_ctx: ContextVar[str] = ContextVar("call_sid", default="-")


def configure_logging(level: str) -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            add_context,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def add_context(_, __, event_dict: dict) -> dict:
    event_dict["request_id"] = request_id_ctx.get()
    event_dict["call_sid"] = call_sid_ctx.get()
    return event_dict


def new_request_id() -> str:
    return str(uuid4())


log = structlog.get_logger()

