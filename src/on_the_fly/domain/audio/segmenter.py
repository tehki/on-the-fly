"""Turns a stream of frames into utterances, under explicit retention bounds.

The segmenter is where captured audio first accumulates, so it is where the retention
argument has to be made precisely rather than waved at.

**Pre-roll is bounded by construction.** Speech is detected a moment after it starts, so a
short ring of recent frames is kept to avoid clipping the first syllable. That ring has a
fixed `maxlen`, which means a frame in it is overwritten after exactly `pre_roll_ms` — no
scheduler needed, and no frame can outlive that bound even if the pipeline stalls. The
configuration refuses a pre-roll longer than the store's retention window, so this bound is
always the tighter of the two.

**Accumulating an utterance is active use.** While frames are being assembled into one
utterance they are required for the operation in progress, which is exactly the condition
Article 6 describes as active use — the post-use clock has not started. `max_utterance_ms`
bounds how long that can last, so "active use" cannot become an unbounded excuse.

**A discarded utterance is deleted immediately.** Audio below `min_utterance_ms` is a cough
or a door, and it is dropped without ever reaching the store. There is nothing to expire
because nothing was retained.

Only the assembled utterance enters `EphemeralStore`, where the ten-second post-use rule
takes over.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.ports import VoiceActivityDetector
from on_the_fly.domain.retention import EphemeralStore, TransientHandle

# Long enough to catch a clipped first syllable, short enough that the ring is a rounding
# error against the retention window.
DEFAULT_PRE_ROLL_MS = 300

# Silence after speech before an utterance is considered finished. Too short cuts people
# off mid-sentence; too long adds straight latency to every translation.
DEFAULT_HANGOVER_MS = 500

# Shorter than this is a cough, a click, or a door.
DEFAULT_MIN_UTTERANCE_MS = 250

# A hard ceiling on one utterance. Someone talking without pause, or a detector stuck on,
# must not grow a buffer without limit (handbook 8).
DEFAULT_MAX_UTTERANCE_MS = 15_000

# Refuses a configuration that would let a single utterance consume unreasonable memory.
ABSOLUTE_MAX_UTTERANCE_MS = 60_000


class EndReason(Enum):
    """Why an utterance ended. Useful operationally, and carries no content."""

    SILENCE = "SILENCE"
    MAX_DURATION = "MAX_DURATION"
    FLUSH = "FLUSH"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Utterance:
    """A completed utterance. The audio lives in the store; this is the reference to it.

    Safe to log, queue, or attach to a correlation record — every field is metadata.
    """

    handle: TransientHandle
    audio_format: AudioFormat
    duration_seconds: float
    frame_count: int
    ended_because: EndReason

    def __str__(self) -> str:
        return (
            f"utterance {self.handle.entry_id} "
            f"({self.duration_seconds:.2f}s, {self.frame_count} frames, "
            f"ended={self.ended_because})"
        )


@dataclass(frozen=True)
class SegmenterConfig:
    """Validated segmentation timings."""

    frame_ms: int = 20
    pre_roll_ms: int = DEFAULT_PRE_ROLL_MS
    hangover_ms: int = DEFAULT_HANGOVER_MS
    min_utterance_ms: int = DEFAULT_MIN_UTTERANCE_MS
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS

    def __post_init__(self) -> None:
        for name in ("frame_ms", "pre_roll_ms", "hangover_ms", "min_utterance_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_utterance_ms <= self.min_utterance_ms:
            raise ValueError("max_utterance_ms must exceed min_utterance_ms")
        if self.max_utterance_ms > ABSOLUTE_MAX_UTTERANCE_MS:
            raise ValueError(
                f"max_utterance_ms of {self.max_utterance_ms} exceeds the "
                f"{ABSOLUTE_MAX_UTTERANCE_MS}ms ceiling; a single utterance must stay bounded"
            )
        if self.hangover_ms < self.frame_ms:
            raise ValueError("hangover_ms must be at least one frame")

    @property
    def pre_roll_frames(self) -> int:
        return max(1, self.pre_roll_ms // self.frame_ms)

    @property
    def hangover_frames(self) -> int:
        return max(1, self.hangover_ms // self.frame_ms)

    @property
    def max_utterance_frames(self) -> int:
        return max(1, self.max_utterance_ms // self.frame_ms)


class UtteranceSegmenter:
    """Feeds frames in, gets utterances out.

    Single-threaded by design. Threading belongs in the capture adapter, where the device
    and the UI event loop actually live; keeping the segmenter synchronous keeps its tests
    deterministic and its state easy to reason about (handbook 7).
    """

    def __init__(
        self,
        store: EphemeralStore,
        detector: VoiceActivityDetector,
        audio_format: AudioFormat,
        *,
        config: SegmenterConfig | None = None,
        label: str = "captured_audio_frames",
    ) -> None:
        self._config = config if config is not None else SegmenterConfig()
        self._store = store
        self._detector = detector
        self._format = audio_format
        self._label = label

        pre_roll_seconds = self._config.pre_roll_ms / 1000.0
        if pre_roll_seconds > store.retention_seconds:
            # The ring is retention-by-construction only while it is the tighter bound.
            raise ValueError(
                f"pre_roll of {pre_roll_seconds}s exceeds the store's "
                f"{store.retention_seconds}s retention window; the pre-roll ring would "
                "become the longer-lived copy and escape the retention rule"
            )

        self._pre_roll: deque[bytes] = deque(maxlen=self._config.pre_roll_frames)
        self._speech: list[bytes] = []
        self._trailing_silence_frames = 0
        self._in_speech = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def buffered_frames(self) -> int:
        """Frames currently held for an in-progress utterance. Bounded by config."""
        return len(self._speech)

    def __repr__(self) -> str:
        return (
            f"UtteranceSegmenter(in_speech={self._in_speech}, "
            f"buffered_frames={len(self._speech)}, pre_roll={len(self._pre_roll)})"
        )

    def push(self, frame: bytes) -> Utterance | None:
        """Feed one frame. Returns an utterance when one has just completed."""
        self._format.validate_frame(frame)

        if self._detector.is_speech(frame):
            return self._on_speech_frame(frame)
        return self._on_silence_frame(frame)

    def _on_speech_frame(self, frame: bytes) -> Utterance | None:
        if not self._in_speech:
            # Speech starts: the pre-roll becomes the head of the utterance so the first
            # syllable is not clipped, and the ring is emptied so those frames exist in
            # exactly one place.
            self._in_speech = True
            self._speech = list(self._pre_roll)
            self._pre_roll.clear()

        self._speech.append(frame)
        self._trailing_silence_frames = 0

        if len(self._speech) >= self._config.max_utterance_frames:
            return self._emit(EndReason.MAX_DURATION)
        return None

    def _on_silence_frame(self, frame: bytes) -> Utterance | None:
        if not self._in_speech:
            # deque(maxlen=...) drops the oldest frame automatically. That drop is the
            # retention bound: nothing here survives longer than pre_roll_ms.
            self._pre_roll.append(frame)
            return None

        # Trailing silence is kept: it is part of the utterance until we know the speaker
        # has actually stopped, and trimming it early clips word endings.
        self._speech.append(frame)
        self._trailing_silence_frames += 1

        if self._trailing_silence_frames >= self._config.hangover_frames:
            return self._emit(EndReason.SILENCE)
        if len(self._speech) >= self._config.max_utterance_frames:
            return self._emit(EndReason.MAX_DURATION)
        return None

    def flush(self) -> Utterance | None:
        """End any in-progress utterance, e.g. when the source stops.

        Emits whatever has accumulated if it is long enough, and discards it otherwise.
        Either way the buffer ends empty: shutting down must not leave captured audio
        sitting in a half-finished utterance (handbook 35).
        """
        if not self._in_speech:
            self._pre_roll.clear()
            return None
        return self._emit(EndReason.FLUSH)

    def _emit(self, reason: EndReason) -> Utterance | None:
        frames = self._speech
        self._speech = []
        self._trailing_silence_frames = 0
        self._in_speech = False

        audio = b"".join(frames)
        duration = self._format.duration_seconds(len(audio))

        if duration * 1000 < self._config.min_utterance_ms:
            # Too short to be speech. Dropped without ever being stored, so there is
            # nothing to expire and nothing to fail to delete.
            return None

        handle = self._store.put(audio, label=self._label)
        return Utterance(
            handle=handle,
            audio_format=self._format,
            duration_seconds=duration,
            frame_count=len(frames),
            ended_because=reason,
        )

    def discard(self) -> None:
        """Drop buffered audio without storing it.

        The counterpart to `flush()`, for when nobody is waiting for the result — a caller
        that stopped consuming, or a session torn down by an exception. Storing the tail
        there would retain audio for ten seconds that no one asked for and no one will
        read, so dropping it is both the more private and the more honest outcome.
        """
        self._pre_roll.clear()
        self._speech = []
        self._trailing_silence_frames = 0
        self._in_speech = False

    def reset(self) -> None:
        """Drop all buffered audio and adaptive state, e.g. on a device change."""
        self.discard()
        self._detector.reset()
