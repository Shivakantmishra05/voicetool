# Railway Deployment Runbook

Use Railway when temporary tunnels like ngrok/Cloudflare are failing from Twilio.
Railway gives the app a stable HTTPS/WSS URL:

`Twilio -> Railway FastAPI -> OpenAI Realtime -> Twilio`

## 1. Create Project

1. Open Railway.
2. Create a new project.
3. Select **Deploy from GitHub repo**.
4. Choose this repo.
5. For the API service, set:

```text
Root Directory: /backend
```

This is important. The Dockerfile is inside `backend/` and expects the build
context to contain:

- `requirements.txt`
- `app/`
- `alembic/`
- `alembic.ini`
- `docker-entrypoint.sh`

Do not deploy from repo root unless you also change the Docker build context.

## 2. Add Postgres

In the same Railway project:

1. Add a PostgreSQL service.
2. Railway will expose `DATABASE_URL`.
3. The app automatically converts Railway's `postgres://...` URL to
   `postgresql+asyncpg://...`.

## 3. Add Redis

Add Redis/Key-Value service if available in your Railway account.

Set:

```text
REDIS_URL=<Railway Redis connection URL>
REQUIRE_REDIS=true
```

If you cannot add Redis for the demo, set:

```text
REQUIRE_REDIS=false
```

But for stable calls, Redis is better because stream-token replay protection and
call memory survive across workers/restarts.

## 4. Required Environment Variables

Set these in the Railway API service:

```text
ENVIRONMENT=staging
PUBLIC_BASE_URL=https://YOUR-RAILWAY-DOMAIN.up.railway.app
LOG_LEVEL=INFO
CORS_ORIGINS=https://YOUR-RAILWAY-DOMAIN.up.railway.app

DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
REQUIRE_REDIS=true

TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+13852131633
STREAM_TOKEN_SECRET=<long-random-secret>

OPENAI_API_KEY=...
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_SUMMARY_MODEL=gpt-4.1-mini
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe

SUPABASE_URL=...
SUPABASE_KEY=...

ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong-password>
ADMIN_SESSION_SECRET=<long-random-secret>

STARTUP_PROVIDER_CHECKS_ENABLED=true
STARTUP_PROVIDER_CHECKS_REQUIRED=false
PROVIDER_CHECK_TIMEOUT_SECONDS=8
```

For `STREAM_TOKEN_SECRET` and `ADMIN_SESSION_SECRET`, generate random values:

```bash
openssl rand -hex 32
```

## 5. Deploy

After variables are set, trigger deploy.

The container start command is already Render/Railway compatible:

```text
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

The entrypoint runs:

```text
alembic upgrade head
```

So Railway Postgres migrations should run automatically on startup.

## 6. Verify Railway URL

Once deploy is live:

```bash
curl -i https://YOUR-RAILWAY-DOMAIN.up.railway.app/health
curl -i https://YOUR-RAILWAY-DOMAIN.up.railway.app/ready
curl -i -X POST https://YOUR-RAILWAY-DOMAIN.up.railway.app/twilio/voice \
  -d 'CallSid=CA_TEST' \
  -d 'From=+919720028490'
```

The last command should return XML with:

```xml
<Connect>
  <Stream url="wss://YOUR-RAILWAY-DOMAIN.up.railway.app/ws/twilio/...">
```

## 7. Update Twilio

In Twilio Console, open the phone number and set:

```text
A call comes in:
Webhook
POST https://YOUR-RAILWAY-DOMAIN.up.railway.app/twilio/voice
```

## 8. Test Outbound Call

From local terminal:

```bash
docker compose exec -T api python -c 'import os; from twilio.rest import Client; c=Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]); call=c.calls.create(to="+919720028490", from_=os.environ["TWILIO_PHONE_NUMBER"], url="https://YOUR-RAILWAY-DOMAIN.up.railway.app/twilio/voice", method="POST"); print(call.sid, call.status)'
```

## 9. Railway Logs To Watch

Expected good sequence:

```text
POST /twilio/voice 200
twilio_stream_token_issued
twilio_ws_auth_ok
call_started
openai_ws_connected
session_update_ack
greeting_started
first_audio_delta
```

## 10. Failure Notes

- If `/ready` fails, check `DATABASE_URL` and migrations.
- If Twilio says application error, check Twilio Debugger for HTTP status.
- If Twilio gets 403, `PUBLIC_BASE_URL` probably does not exactly match the Railway domain used by Twilio.
- If no voice plays, check WebSocket logs: `twilio_ws_auth_ok`, `call_started`, `openai_ws_connected`.
- If CRM insert fails, Supabase RLS/key is the issue, not Railway.

