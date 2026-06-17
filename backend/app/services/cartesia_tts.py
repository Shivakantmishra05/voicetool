import io
import wave

import httpx

from app.config import Settings
from app.utils.logging import log


class CartesiaTTS:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize_ulaw(self, text: str, *, call_sid: str | None = None) -> bytes:
        if not self.settings.cartesia_api_key:
            raise RuntimeError("CARTESIA_API_KEY is not configured")
        if not self.settings.cartesia_voice_id:
            raise RuntimeError("CARTESIA_VOICE_ID is not configured")

        payload = {
            "model_id": self.settings.cartesia_model_id,
            "transcript": text,
            "voice": {
                "mode": "id",
                "id": self.settings.cartesia_voice_id,
            },
            "output_format": {
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": self.settings.cartesia_sample_rate,
            },
            "language": self.settings.cartesia_language,
            "generation_config": {
                "speed": 1,
                "volume": 1,
            },
        }
        headers = {
            "Cartesia-Version": self.settings.cartesia_version,
            "X-API-Key": self.settings.cartesia_api_key,
            "Content-Type": "application/json",
        }

        log.info(
            "cartesia_tts_request",
            call_sid=call_sid,
            model_id=self.settings.cartesia_model_id,
            voice_id_suffix=self.settings.cartesia_voice_id[-6:],
            chars=len(text),
            output_container="wav",
            output_encoding="pcm_s16le",
            output_sample_rate=self.settings.cartesia_sample_rate,
        )
        async with httpx.AsyncClient(timeout=self.settings.tts_timeout_seconds) as client:
            response = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            log.error(
                "cartesia_tts_failed",
                call_sid=call_sid,
                status_code=response.status_code,
                body_preview=response.text[:500],
            )
            response.raise_for_status()

        ulaw = wav_pcm_to_ulaw_8khz(response.content)
        log.info(
            "cartesia_tts_success",
            call_sid=call_sid,
            wav_bytes=len(response.content),
            ulaw_bytes=len(ulaw),
        )
        return ulaw


def wav_pcm_to_ulaw_8khz(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        frame_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if sample_width != 2:
        raise ValueError(f"Cartesia WAV sample width must be 16-bit PCM, got {sample_width * 8}-bit")
    if frame_rate != 8000:
        raise ValueError(f"Cartesia WAV sample rate must be 8000 Hz for Twilio, got {frame_rate}")
    return pcm16le_to_ulaw(frames, channels=channels)


def pcm16le_to_ulaw(pcm: bytes, *, channels: int = 1) -> bytes:
    if channels < 1:
        raise ValueError("WAV channel count must be >= 1")
    frame_size = channels * 2
    if len(pcm) % frame_size:
        pcm = pcm[: len(pcm) - (len(pcm) % frame_size)]

    ulaw = bytearray()
    for offset in range(0, len(pcm), frame_size):
        if channels == 1:
            sample = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        else:
            total = 0
            for channel in range(channels):
                channel_offset = offset + (channel * 2)
                total += int.from_bytes(pcm[channel_offset : channel_offset + 2], "little", signed=True)
            sample = int(total / channels)
        ulaw.append(linear16_to_ulaw(sample))
    return bytes(ulaw)


def linear16_to_ulaw(sample: int) -> int:
    bias = 0x84
    clip = 32635
    if sample < 0:
        sample = -sample
        sign = 0x80
    else:
        sign = 0
    if sample > clip:
        sample = clip
    sample += bias

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF
