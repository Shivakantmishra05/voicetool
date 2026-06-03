# Render Deployment Runbook

This deploy removes the ngrok/Cloudflare tunnel from Twilio's call path.
Twilio should call Render directly:

`Twilio -> https://dreamhome-voice-api.onrender.com/twilio/voice -> WSS media stream -> OpenAI Realtime`

## 1. Push Code

Commit and push the repo to GitHub.

## 2. Create Render Blueprint

1. Open Render Dashboard.
2. Click **New +**.
3. Select **Blueprint**.
4. Connect this GitHub repo.
5. Render should detect `render.yaml`.
6. Create the services.

The blueprint creates:

- `dreamhome-voice-api` web service
- `dreamhome-postgres` database
- `dreamhome-redis` key-value store

## 3. Add Secret Environment Variables

Render will ask for `sync: false` values. Add these from your local `.env`:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`
- `OPENAI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `ADMIN_PASSWORD`

For best CRM reliability, use a Supabase **service role** key for `SUPABASE_KEY`.
If you use a publishable key, Supabase RLS must allow inserts into `calls`.

## 4. Confirm Public URL

The blueprint sets:

`PUBLIC_BASE_URL=https://dreamhome-voice-api.onrender.com`

After deploy, open:

```bash
curl -i https://dreamhome-voice-api.onrender.com/health
curl -i https://dreamhome-voice-api.onrender.com/ready
```

If Render gives your service a different URL, update `PUBLIC_BASE_URL` in Render
to the exact deployed URL and redeploy.

## 5. Update Twilio Webhook

In Twilio Console, open your phone number and set:

```text
A call comes in:
Webhook
POST https://dreamhome-voice-api.onrender.com/twilio/voice
```

Or update by CLI/API after the Render service is live.

## 6. Test Full Call

Run locally or from any machine with Twilio credentials:

```bash
python -c 'import os; from twilio.rest import Client; c=Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]); call=c.calls.create(to="+919720028490", from_=os.environ["TWILIO_PHONE_NUMBER"], url="https://dreamhome-voice-api.onrender.com/twilio/voice", method="POST"); print(call.sid, call.status)'
```

Expected Render logs:

```text
POST /twilio/voice 200
twilio_ws_auth_ok
call_started
openai_ws_connected
session_update_ack
greeting_started
first_audio_delta
```

## 7. Important Demo Notes

- Do not use free tunnel URLs for the demo.
- Do not use Render free web service for serious demos; cold starts can break phone calls.
- Keep one paid/starter instance always warm.
- Twilio needs stable HTTPS and WSS reachability.
- If Twilio says "application error", check Twilio Debugger first for HTTP status.

