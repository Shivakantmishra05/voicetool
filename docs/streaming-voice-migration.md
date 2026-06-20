# DreamHome Streaming Voice Migration

Goal: replace OpenAI Realtime audio with a lower-cost, lower-latency streaming pipeline:

Twilio Media Streams -> Deepgram Flux/Nova Streaming STT -> GPT-4.1-mini Streaming -> Cartesia Sonic Streaming TTS -> Twilio Media Output

This must be rolled out behind a provider flag. Do not delete the current OpenAI Realtime path until the new path has passed live-call tests.

## Current State

- `backend/app/websocket/twilio_media.py` is the active call bridge.
- OpenAI Realtime currently owns STT, turn events, response generation, and optional audio fallback.
- `backend/app/services/stt.py` already contains a Deepgram streaming STT client using 8 kHz mu-law input.
- `backend/app/services/llm.py` currently contains Gemini streaming helpers, not OpenAI text streaming.
- `backend/app/services/cartesia_tts.py` currently uses Cartesia HTTP bytes TTS, not Cartesia WebSocket streaming.
- CRM, memory, lead extraction, stage detection, summaries, and Twilio webhook flow should stay unchanged.

## Target Runtime Flow

1. Twilio sends `media.payload` as base64 G.711 mu-law, 8 kHz.
2. Decode payload to bytes and send raw mu-law bytes to Deepgram.
3. Deepgram emits speech-start events and interim/final transcripts.
4. On speech start:
   - stop active Cartesia TTS stream
   - cancel active GPT stream
   - send Twilio `clear`
   - reset assistant-speaking state
5. On final transcript:
   - append user transcript
   - run existing fact extraction, memory update, language detection, stage detection, objections, lead score
   - build existing dynamic prompt context
   - start GPT-4.1-mini streaming text generation
6. Buffer streamed LLM tokens until a sentence boundary:
   - `.`, `?`, `!`, Hindi danda `।`
   - or soft boundary at 80-120 chars
7. Send first sentence immediately to Cartesia streaming TTS.
8. Cartesia returns 8 kHz mu-law audio chunks, already Twilio-compatible.
9. Send each chunk to Twilio `media.payload`.
10. Send Twilio `mark` at end of assistant turn.

## Required Environment Variables

```env
VOICE_PIPELINE=text_streaming

DEEPGRAM_API_KEY=...
DEEPGRAM_MODEL=flux-general-en
DEEPGRAM_LANGUAGE=hi-Latn
DEEPGRAM_ENDPOINTING_MS=350
DEEPGRAM_UTTERANCE_END_MS=1000
DEEPGRAM_KEEPALIVE_SECONDS=5
DEEPGRAM_RECONNECT_ATTEMPTS=2

OPENAI_TEXT_MODEL=gpt-4.1-mini
OPENAI_TEXT_TIMEOUT_SECONDS=4
OPENAI_TEXT_MAX_TOKENS=80

CARTESIA_API_KEY=...
CARTESIA_MODEL_ID=sonic-3.5
CARTESIA_VOICE_ID=56e35e2d-6eb6-4226-ab8b-9776515a7094
CARTESIA_VERSION=2026-03-01
CARTESIA_LANGUAGE=hi
CARTESIA_SAMPLE_RATE=8000
CARTESIA_ENCODING=pcm_mulaw
```

Keep the old variables during rollout:

```env
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_REALTIME_VOICE=marin
TTS_PROVIDER=openai
```

## File-by-File Changes

### `backend/app/config/settings.py`

Add:

- `voice_pipeline: str = "openai_realtime"`
- `openai_text_model: str = "gpt-4.1-mini"`
- `openai_text_timeout_seconds: float = 4.0`
- `openai_text_max_tokens: int = 80`
- `cartesia_encoding: str = "pcm_mulaw"`

Validation:

- `VOICE_PIPELINE` must be `openai_realtime` or `text_streaming`.
- If `VOICE_PIPELINE=text_streaming`, require Deepgram key, OpenAI key, Cartesia key, Cartesia voice ID.
- Require Cartesia sample rate `8000`.
- Require Cartesia encoding `pcm_mulaw`.

### `backend/app/services/openai_text_llm.py`

Create a streaming OpenAI text LLM client.

Responsibilities:

- Call OpenAI Responses API or Chat Completions streaming with `gpt-4.1-mini`.
- Accept:
  - system instructions
  - recent transcript turns
  - dynamic memory context
- Yield text deltas.
- Support cancellation via task cancellation.
- Timeout after `OPENAI_TEXT_TIMEOUT_SECONDS`.
- Log:
  - `openai_text_stream_started`
  - `openai_text_first_delta`
  - `openai_text_stream_completed`
  - `openai_text_stream_cancelled`
  - `openai_text_stream_failed`

### `backend/app/services/cartesia_streaming_tts.py`

Create a Cartesia WebSocket TTS client.

Responsibilities:

- Connect once per assistant turn, or reuse per call if stable.
- Send sentence fragments as soon as they are ready.
- Receive audio chunks.
- Output Twilio-ready base64 mu-law payloads.
- Close/cancel immediately on barge-in.
- Log:
  - `cartesia_ws_connect_started`
  - `cartesia_ws_connected`
  - `cartesia_first_audio_chunk`
  - `cartesia_stream_completed`
  - `cartesia_stream_cancelled`
  - `cartesia_stream_failed`

Important:

- Do not request WAV for live calls.
- Do not request 44.1 kHz PCM for Twilio.
- Use 8 kHz mu-law if Cartesia supports it directly.
- If Cartesia only returns PCM16, transcode to 8 kHz mu-law before Twilio.

### `backend/app/services/sentence_stream.py`

Create a tiny sentence-fragmenter.

Rules:

- Emit on `.`, `?`, `!`, `।`
- Emit if buffer exceeds 100 chars and ends near whitespace
- Do not wait for full LLM response
- Avoid sending very tiny fragments unless it is a natural reaction: `Achha.`, `Haan ji.`, `Samajh gayi.`

### `backend/app/websocket/twilio_text_streaming.py`

Create the new call bridge.

Responsibilities:

- Accept the same Twilio websocket messages.
- Use existing call auth and call repository.
- Send Twilio media to Deepgram.
- On Deepgram final transcript, reuse the existing memory/CRM logic from `TwilioMediaSession`.
- Start OpenAI text streaming.
- Pipe sentence fragments into Cartesia streaming TTS.
- Send Cartesia audio chunks to Twilio.
- Implement barge-in cancellation.
- Persist transcript and CRM summary using existing `_summarize_and_persist_call()` behavior.

### `backend/app/websocket/twilio_media.py`

Do not delete yet.

Add a routing layer:

- If `VOICE_PIPELINE=openai_realtime`, use current class.
- If `VOICE_PIPELINE=text_streaming`, use the new text-streaming class.

### `backend/app/api/twilio.py`

No Twilio URL change required.

Keep:

- `/twilio/voice`
- media stream URL
- stream token auth

Only change the websocket handler dispatch based on `VOICE_PIPELINE`.

## Barge-In Design

State flags:

- `caller_speaking`
- `assistant_speaking`
- `llm_task`
- `tts_task`
- `tts_cancel_event`
- `twilio_pending_marks`

On Deepgram speech started:

1. `caller_speaking=True`
2. Cancel `llm_task` if active
3. Cancel/close Cartesia websocket
4. Send Twilio `clear`
5. Set `assistant_speaking=False`
6. Ignore late TTS chunks for the old response ID

Do not wait for final transcript to clear audio.

## Latency Targets

Expected best case:

- Deepgram endpoint/final: 350-900 ms after user stops
- GPT first token: 200-600 ms
- First sentence ready: 300-900 ms
- Cartesia first audio: 200-700 ms
- Total response latency: 1.1-2.5 seconds

Sub-2 seconds is realistic only when:

- prompts are short
- first sentence is short
- endpointing is not too slow
- Cartesia returns streaming audio, not whole WAV
- server region is close to Twilio media region

## Cost Reduction

Current OpenAI Realtime audio is expensive because one provider handles continuous audio in/out.

New architecture reduces cost by:

- Deepgram billed only for STT minutes
- GPT-4.1-mini billed only for text tokens
- Cartesia billed only for generated assistant speech
- no OpenAI audio output tokens

Expected result:

- Much lower LLM cost
- TTS cost depends on voice/provider
- More controllable than OpenAI Realtime

## Rollout Plan

### Phase 1: Parallel Path

- Add new services.
- Keep OpenAI Realtime path untouched.
- Add `VOICE_PIPELINE=text_streaming`.
- Test with local calls only.

### Phase 2: One Demo Number

- Deploy to Railway with `VOICE_PIPELINE=text_streaming`.
- Make 10 test calls.
- Confirm:
  - first response latency
  - barge-in works
  - no static audio
  - no stuck in-progress call
  - CRM save still works

### Phase 3: Production Default

- Make text streaming default.
- Keep OpenAI Realtime as emergency fallback for one release.

### Phase 4: Remove OpenAI Realtime

- Delete OpenAI realtime connect/session/audio code only after stable production logs.

## Production Verification Checklist

- `deepgram_connected`
- `deepgram_speech_started`
- `deepgram_final_transcript`
- `openai_text_first_delta`
- `sentence_fragment_emitted`
- `cartesia_first_audio_chunk`
- `twilio_audio_first_payload`
- `barge_in_confirmed`
- `twilio_clear_sent`
- `call_completion_saved`
- `crm_outbox_delivered`

## Do Not Do

- Do not send WAV to Twilio.
- Do not send 44.1 kHz PCM to Twilio.
- Do not wait for full LLM response before TTS.
- Do not remove existing CRM/memory code.
- Do not delete OpenAI Realtime path before the new path is proven.
- Do not make first production deployment without a feature flag.
