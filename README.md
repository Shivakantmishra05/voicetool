# DreamHome Properties AI Voice Calling Agent

Production-grade FastAPI backend for inbound real-estate voice calls using Twilio Media Streams, Deepgram realtime STT, Gemini low-latency reasoning, ElevenLabs streaming TTS, PostgreSQL, Redis, and a Bootstrap operations dashboard.

## Architecture

Twilio receives the inbound call and requests `POST /twilio/voice`. The app returns TwiML using `<Connect><Stream>`, which is required for bidirectional media. Twilio then opens `wss://.../ws/twilio`.

Realtime flow:

1. Twilio sends base64 `audio/x-mulaw` 8 kHz caller audio.
2. The websocket session forwards raw mu-law bytes to Deepgram streaming STT.
3. Final transcripts update per-call memory in Redis, with local memory fallback.
4. Gemini generates short phone-call responses using the DreamHome sales prompt.
5. ElevenLabs streams `pcm_16000` audio for free-tier compatibility.
6. The app transcodes PCM16/16 kHz to Twilio-safe mu-law/8 kHz, then sends `media` plus `mark` messages.
7. If Deepgram detects speech while AI audio is playing, the app sends Twilio `clear` and cancels the pending response task for barge-in.
8. Transcript turns and call metadata are persisted to PostgreSQL.
9. On call end, Gemini creates CRM JSON with summary, sentiment, lead status, outcome, and extracted lead fields.

Provider choices:

- FastAPI over Flask: native async, websocket support, dependency injection, and OpenAPI.
- Twilio bidirectional Media Streams: Twilio’s supported way to send audio back into an active phone call.
- Deepgram STT: low-latency streaming, phone-audio support, interim/final transcripts, endpointing, VAD events.
- Gemini 2.5 Flash Lite by default: current low-latency/high-throughput Gemini model for short conversational reasoning. Use `gemini-2.5-flash` if quality matters more than latency.
- ElevenLabs `eleven_flash_v2_5` + `pcm_16000`: keeps the streaming endpoint usable on lower tiers, with local mu-law transcoding for Twilio playback.
- PostgreSQL for production persistence; SQLite is only suitable for local smoke tests.
- Redis for horizontal scaling of call state; local memory fallback keeps development resilient.

## Folder Structure

```text
backend/app/
  api/          HTTP routes for health, Twilio webhooks, dashboard
  config/       typed environment settings
  database/     async SQLAlchemy setup
  middleware/   request IDs and rate limiting
  models/       call and transcript schema
  prompts/      sales and summary prompts
  services/     STT, LLM, TTS, memory, repositories, retries
  static/       dashboard CSS
  telephony/    TwiML and Twilio validation
  templates/    Bootstrap dashboard
  websocket/    Twilio realtime media orchestrator
```

## Setup

1. Copy environment file:

```bash
cp .env.example .env
```

2. Fill real credentials in `.env`:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `DEEPGRAM_API_KEY`
- `GEMINI_API_KEY`
- `ELEVENLABS_API_KEY`
- `ELEVENLABS_VOICE_ID`
- `PUBLIC_BASE_URL`

3. Start services:

```bash
docker compose up --build
```

The API container runs `alembic upgrade head` before Uvicorn starts. For managed deployments, keep migrations in CI/CD and run them once per release.

4. Apply migrations manually only when needed:

```bash
docker compose run --rm api alembic upgrade head
```

The app also creates tables on first local startup for developer convenience, but production deploys should use Alembic migrations.

5. Check health, readiness, metrics, and startup diagnostics:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/metrics
curl http://localhost:8000/startup-diagnostics
```

6. Open dashboard:

```text
http://localhost:8000/
```

Sign in with `ADMIN_USERNAME` and `ADMIN_PASSWORD` from `.env`.

## Twilio Setup

For local testing, expose the API:

```bash
ngrok http 8000
```

Set `PUBLIC_BASE_URL` to the HTTPS ngrok URL and restart the app.

In Twilio Console:

- Buy or select a Voice-capable number.
- Set incoming call webhook to `POST https://your-domain/twilio/voice`.
- Keep the webhook method as `POST`.
- Save the Twilio number, restart the API, then call the number from a mobile phone.

The returned TwiML uses:

```xml
<Connect>
  <Stream url="wss://your-domain/ws/twilio?token=..." />
</Connect>
```

The token is short-lived, signed, and bound to the Twilio `CallSid`; direct websocket connections without a valid token are rejected before `accept()`.

## First Test Call Checklist

Before calling:

- `docker compose ps` shows `api`, `postgres`, and `redis` running.
- `/ready` returns `{"status":"ready"}`.
- `/startup-diagnostics` shows provider checks. If a provider is `failed`, fix that before testing voice.
- Twilio webhook URL exactly matches the ngrok HTTPS URL plus `/twilio/voice`.
- `PUBLIC_BASE_URL` has no trailing path.

During the call:

- You should hear the greeting within a few seconds.
- When you interrupt the agent, logs should show `barge_in_detected` and `twilio_clear_sent`.
- When AI audio finishes, logs should show `twilio_mark_ack`.
- `/metrics` should show counters such as `voice_twilio_media_frames_total`, `voice_final_transcripts_total`, and latency summaries.

## Security Notes

- Secrets are read only from environment variables.
- Twilio webhook signature validation is enforced outside `ENVIRONMENT=local`.
- Twilio websocket streams use signed, short-lived, CallSid-bound stream tokens.
- Dashboard and CSV export require an admin session and CSRF-protected login/logout.
- Webhook request rate limiting is enabled.
- Structured JSON logs include request and call IDs.
- Do not log provider API keys or raw audio.
- Run behind HTTPS/WSS in production.

## Reliability Behavior

- Redis outage falls back to process memory unless `REQUIRE_REDIS=true`.
- Gemini calls retry once and fall back to a safe recovery sentence.
- ElevenLabs calls retry once and then send short mu-law silence instead of crashing the websocket.
- Deepgram websocket closure is logged and the Twilio session continues until call stop/timeout.
- Twilio barge-in uses generation IDs, bounded outbound queues, `clear`, and `mark` acknowledgements to stop stale buffered audio.
- Deepgram keepalives protect calls during quiet periods.
- Conversation memory is bounded by turn count and transcript character budget.

## Scaling Notes

Use multiple API replicas only when Redis is available. Websocket connections are stateful during the call, so route stickiness is recommended at the load balancer. PostgreSQL should run as a managed database in production. For heavy post-call analytics, move summarization/export jobs to Celery, Dramatiq, or a managed queue.

Latency bottlenecks are usually:

- LLM first token latency
- TTS first audio latency
- network distance between Twilio, your server, and providers
- response text length

Keep responses under two short sentences. `pcm_16000` adds lightweight local transcoding but avoids paid/telephony-format account restrictions.

## Troubleshooting

- No call audio back: confirm TwiML uses `<Connect><Stream>`, not `<Start><Stream>`.
- Twilio warning 31950: outbound messages must be JSON text with base64 raw mu-law 8 kHz audio, not binary websocket frames.
- Websocket not connecting: `PUBLIC_BASE_URL` must be public HTTPS so the derived websocket URL is WSS.
- Websocket closes with policy violation: the stream token is missing, expired, replayed, or bound to another `CallSid`.
- Twilio signature failures: make sure the externally visible webhook URL exactly matches the URL Twilio signs.
- Bad transcripts: confirm Deepgram is receiving raw decoded mu-law bytes from Twilio payloads.
- No final transcripts: increase `DEEPGRAM_ENDPOINTING_MS` or inspect Deepgram provider diagnostics.
- Delayed agent speech: check `voice_llm_first_token_ms_avg`, `voice_tts_first_byte_ms_avg`, and `voice_response_first_audio_ms_avg`.
- Agent talks over caller: inspect `twilio_mark_ack`, `barge_in_detected`, and `twilio_clear_sent` logs.
- Robotic or long answers: lower Gemini max output tokens and keep the system prompt strict.

## Production Deployment

Recommended baseline:

- HTTPS load balancer with websocket support.
- 2+ API replicas, sticky websocket routing, graceful shutdown.
- Managed PostgreSQL with backups.
- Managed Redis with persistence disabled or tuned for ephemeral state.
- Secret manager for all provider keys.
- Centralized log sink and metrics scraper.
- Alert on websocket error rate, provider latency, failed calls, and empty summaries.

Before going live, complete provider-side compliance checks for call recording, consent, data retention, DND/telemarketing rules, and WhatsApp follow-up permissions.
