import base64

MU_LAW_BIAS = 0x84
MU_LAW_CLIP = 32635


class Pcm16ToUlaw8kTranscoder:
    def __init__(self, input_sample_rate: int = 16000):
        if input_sample_rate != 16000:
            raise ValueError("only 16 kHz PCM input is supported")
        self._pending = b""
        self._ulaw_buffer = bytearray()

    def transcode_chunk_to_base64_frames(self, chunk: bytes, frame_bytes: int = 160) -> list[str]:
        if not chunk:
            return []
        raw = self._pending + chunk
        usable = len(raw) - (len(raw) % 4)
        self._pending = raw[usable:]
        if usable <= 0:
            return []

        for offset in range(0, usable, 4):
            first = int.from_bytes(raw[offset : offset + 2], "little", signed=True)
            second = int.from_bytes(raw[offset + 2 : offset + 4], "little", signed=True)
            self._ulaw_buffer.append(linear16_to_ulaw((first + second) // 2))

        return self._pop_frames(frame_bytes)

    def flush_base64_frames(self, frame_bytes: int = 160) -> list[str]:
        if not self._ulaw_buffer:
            return []
        self._ulaw_buffer.extend(b"\xff" * (frame_bytes - len(self._ulaw_buffer)))
        return self._pop_frames(frame_bytes)

    def _pop_frames(self, frame_bytes: int) -> list[str]:
        frames = []
        while len(self._ulaw_buffer) >= frame_bytes:
            frame = bytes(self._ulaw_buffer[:frame_bytes])
            del self._ulaw_buffer[:frame_bytes]
            frames.append(base64.b64encode(frame).decode("ascii"))
        return frames


def linear16_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sign:
        sample = -sample
    sample = min(sample, MU_LAW_CLIP) + MU_LAW_BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF
