import asyncio
import io
import platform
import threading
from dataclasses import dataclass, field
from typing import List, Tuple

import edge_tts
from pydub import AudioSegment

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from narration_prep import (
    NarrationUnit,
    pause_seconds_for_chars,
    CUE_BREATH_PAUSE_SECONDS,
)

AVAILABLE_VOICES = [
    {"id": "en-US-JennyNeural", "label": "American Female"},
    {"id": "en-US-AndrewNeural", "label": "American Male"},

    {"id": "en-IN-NeerjaNeural", "label": "Indian Female"},
    {"id": "en-IN-PrabhatNeural", "label": "Indian Male"},

    {"id": "en-GB-SoniaNeural", "label": "English Female"},
    {"id": "en-GB-RyanNeural", "label": "English Male"},
]

VOICE_IDS = {v["id"] for v in AVAILABLE_VOICES}
DEFAULT_VOICE = AVAILABLE_VOICES[0]["id"]

"""
How many NarrationUnits (roughly) to batch into a single edge-tts
request before splicing in pauses. Units are short (one cluster or one
cue each) so we batch several per actual network call to avoid firing
a flood of tiny requests (the 403 risk), while still keeping each
synthesized chunk short enough to generate well within the time it
takes to play the previous chunk.
"""
UNITS_PER_CHUNK = 6

"""
Short internal pause inserted *before* every punctuation-name segment
within a unit (e.g. between "...concerning" and "full stop"). Without
this, a unit's spoken_text is one continuous TTS phrase and the model has
no cue that a mark belongs there, so it reads as if "full stop" were just
the next word in the sentence. This is independent of, and much shorter
than, the writing/breath pause that already follows the whole unit.
"""
INTRA_UNIT_PUNCT_PAUSE_MS = 150


@dataclass
class AudioChunk:
    index: int
    audio_bytes: bytes
    units: List[NarrationUnit]
    duration_ms: int
    unit_starts_ms: List[int] = field(default_factory=list)


def group_units_into_chunks(units: List[NarrationUnit], units_per_chunk: int = UNITS_PER_CHUNK) -> List[List[NarrationUnit]]:
    chunks = []
    for i in range(0, len(units), units_per_chunk):
        chunks.append(units[i:i + units_per_chunk])
    return chunks


async def _synthesize_unit(text: str, rate: str, voice: str) -> AudioSegment:
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    buf = io.BytesIO()
    async for message in communicate.stream():
        if message["type"] == "audio":
            buf.write(message["data"])
    buf.seek(0)
    if buf.getbuffer().nbytes == 0:
        return AudioSegment.silent(duration=50)
    return AudioSegment.from_file(buf, format="mp3")


async def _synthesize_unit_audio(unit: NarrationUnit, rate_str: str, voice: str) -> AudioSegment:
    segment_audio = AudioSegment.silent(duration=0)
    for text, is_punct_name in unit.spoken_segments:
        text = text.strip()
        if not text:
            continue
        if is_punct_name:
            segment_audio += AudioSegment.silent(duration=INTRA_UNIT_PUNCT_PAUSE_MS)
        segment_audio += await _synthesize_unit(text, rate_str, voice)
    return segment_audio


async def _synthesize_chunk_async(
    unit_group: List[NarrationUnit], chars_per_minute: float, speech_rate_pct: int, voice: str
) -> Tuple[AudioSegment, List[int]]:
    rate_str = f"{speech_rate_pct:+d}%"
    combined = AudioSegment.silent(duration=0)
    starts: List[int] = []

    for unit in unit_group:
        starts.append(len(combined))

        if not unit.spoken_segments:
            continue
        segment = await _synthesize_unit_audio(unit, rate_str, voice)
        combined += segment

        if unit.kind == "cue":
            pause_s = CUE_BREATH_PAUSE_SECONDS
        else:
            pause_s = pause_seconds_for_chars(unit.pause_after_chars, chars_per_minute)
        combined += AudioSegment.silent(duration=int(pause_s * 1000))

    return combined, starts


_thread_local = threading.local()


def _get_thread_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop


def synthesize_chunk(
    unit_group: List[NarrationUnit], chars_per_minute: float, speech_rate_pct: int, voice: str = DEFAULT_VOICE
) -> AudioChunk:
    loop = _get_thread_loop()
    asyncio.set_event_loop(loop)
    segment, starts = loop.run_until_complete(
        _synthesize_chunk_async(unit_group, chars_per_minute, speech_rate_pct, voice)
    )

    out_buf = io.BytesIO()
    segment.export(out_buf, format="mp3")
    return AudioChunk(
        index=-1,
        audio_bytes=out_buf.getvalue(),
        units=unit_group,
        duration_ms=len(segment),
        unit_starts_ms=starts,
    )


class NarrationSession:
    def __init__(
        self,
        units: List[NarrationUnit],
        chars_per_minute: float = 150.0,
        speech_rate_pct: int = 0,
        voice: str = DEFAULT_VOICE,
    ):
        self.units = units
        self.chars_per_minute = chars_per_minute
        self.speech_rate_pct = speech_rate_pct
        self.voice = voice if voice in VOICE_IDS else DEFAULT_VOICE
        self.chunk_groups: List[List[NarrationUnit]] = group_units_into_chunks(units)
        self.total_chunks = len(self.chunk_groups)

        self._cache: dict[int, AudioChunk] = {}
        self._lock = threading.Lock()
        self._generating: set[int] = set()
        self._condition = threading.Condition(self._lock)

        self._epoch = 0

    def update_settings(
        self, chars_per_minute: float = None, speech_rate_pct: int = None, voice: str = None
    ):
        with self._lock:
            if chars_per_minute is not None:
                self.chars_per_minute = chars_per_minute
            if speech_rate_pct is not None:
                self.speech_rate_pct = speech_rate_pct
            if voice is not None and voice in VOICE_IDS:
                self.voice = voice
            self._epoch += 1
            self._cache.clear()

    update_speed = update_settings

    def _claim_for_generation(self, index: int) -> bool:
        with self._lock:
            if index in self._cache or index in self._generating:
                return False
            self._generating.add(index)
            return True

    def prefetch_async(self, index: int):
        if index < 0 or index >= self.total_chunks:
            return
        if not self._claim_for_generation(index):
            return
        t = threading.Thread(target=self._safe_background_generation, args=(index,), daemon=True)
        t.start()

    def _safe_background_generation(self, index: int):
        try:
            self._run_claimed_generation(index)
        except Exception as e:
            print(f"[tts] background generation of chunk {index} failed: {type(e).__name__}: {e}")

    def _run_claimed_generation(self, index: int) -> AudioChunk:
        with self._lock:
            cpm = self.chars_per_minute
            rate = self.speech_rate_pct
            voice = self.voice
            epoch = self._epoch
        group = self.chunk_groups[index]
        try:
            chunk = synthesize_chunk(group, cpm, rate, voice)
            chunk.index = index
        except Exception:
            with self._lock:
                self._generating.discard(index)
                self._condition.notify_all()
            raise
        with self._lock:
            self._generating.discard(index)
            if epoch == self._epoch:
                self._cache[index] = chunk
            self._condition.notify_all()
        return chunk

    def get_chunk(self, index: int, timeout: float = 60.0) -> AudioChunk:
        if index < 0 or index >= self.total_chunks:
            raise IndexError(f"chunk {index} out of range (0..{self.total_chunks - 1})")

        chunk = None
        for _ in range(2):
            if self._claim_for_generation(index):
                chunk = self._run_claimed_generation(index)
            else:
                with self._condition:
                    while index in self._generating:
                        if not self._condition.wait(timeout=timeout):
                            raise TimeoutError(f"Timed out waiting for chunk {index}")
                    chunk = self._cache.get(index)
            if chunk is not None:
                break

        if chunk is None:
            raise RuntimeError(f"Chunk {index} failed to generate")

        self.prefetch_async(index + 1)
        return chunk
