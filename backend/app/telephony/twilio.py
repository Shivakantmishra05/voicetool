from html import escape

from app.config import Settings


def inbound_stream_twiml(settings: Settings, caller_number: str | None = None, stream_token: str | None = None) -> str:
    token_path = f"/{stream_token}" if stream_token else ""
    ws_url = escape(settings.websocket_url.rstrip("/") + token_path)
    stream_status_url = escape(str(settings.public_base_url).rstrip("/") + "/twilio/stream-status")
    caller = escape(caller_number or "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" statusCallback="{stream_status_url}" statusCallbackMethod="POST">
      <Parameter name="company" value="DreamHome Properties"/>
      <Parameter name="From" value="{caller}"/>
      <Parameter name="tokenTransport" value="path"/>
    </Stream>
  </Connect>
</Response>"""
