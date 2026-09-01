"""Drives a capture source through segmentation and out as utterances.

The orchestration layer: it owns the loop, the cleanup, and the counters. It owns no
device, no threads and no framework — those live in adapters, so this stays the same code
on a laptop and on a phone (ADR 0002).

Cleanup is the part worth reading. Whatever ends the session — the source stopping, an
exception, the caller breaking out of the loop — the segmenter is flushed and the source is
closed. A session that exits leaving half an utterance buffered has retained captured audio
outside any deadline, which is the failure this whole layer exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import TracebackType

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.ports import AudioSource, VoiceActivityDetector
from on_the_fly.domain.audio.segmenter import SegmenterConfig, Utterance, UtteranceSegmenter
from on_the_fly.domain.retention import EphemeralStore

# A device that produces malformed buffers occasionally is a nuisance; one that does it
# continuously is broken, and continuing to read from it is not resilience.
DEFAULT_MAX_CONSECUTIVE_INVALID_FRAMES = 10


class CaptureError(Exception):
    """The capture source failed in a way the session cannot continue through."""


@dataclass(frozen=True)
class CaptureStats:
    """Operational counters. `OPERATIONAL_METADATA`: counts and durations, never content."""

    frames_read: int = 0
    frames_invalid: int = 0
    utterances_emitted: int = 0
    audio_seconds_seen: float = 0.0

    def __str__(self) -> str:
        return (
            f"frames_read={self.frames_read} frames_invalid={self.frames_invalid} "
            f"utterances={self.utterances_emitted} "
            f"audio_seconds={self.audio_seconds_seen:.1f}"
        )


class CaptureSession:
    """Reads frames from a source and yields completed utterances."""

    def __init__(
        self,
        source: AudioSource,
        detector: VoiceActivityDetector,
        store: EphemeralStore,
        *,
        config: SegmenterConfig | None = None,
        max_consecutive_invalid_frames: int = DEFAULT_MAX_CONSECUTIVE_INVALID_FRAMES,
    ) -> None:
        if max_consecutive_invalid_frames <= 0:
            raise ValueError("max_consecutive_invalid_frames must be positive")

        self._source = source
        self._store = store
        self._format: AudioFormat = source.audio_format
        self._segmenter = UtteranceSegmenter(
            store=store, detector=detector, audio_format=self._format, config=config
        )
        self._max_consecutive_invalid = max_consecutive_invalid_frames

        self._frames_read = 0
        self._frames_invalid = 0
        self._utterances_emitted = 0
        self._audio_bytes_seen = 0
        self._closed = False

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    @property
    def buffered_frames(self) -> int:
        """Frames held for an in-progress utterance.

        Exposed so "a finished session leaves no captured audio buffered" is an assertion
        a test can make rather than a claim a docstring makes. Metadata only — a count.
        """
        return self._segmenter.buffered_frames

    @property
    def stats(self) -> CaptureStats:
        return CaptureStats(
            frames_read=self._frames_read,
            frames_invalid=self._frames_invalid,
            utterances_emitted=self._utterances_emitted,
            audio_seconds_seen=self._format.duration_seconds(self._audio_bytes_seen),
        )

    def __repr__(self) -> str:
        return f"CaptureSession({self.stats}, closed={self._closed})"

    def utterances(self) -> Iterator[Utterance]:
        """Yield utterances until the source ends.

        Two exits, treated differently on purpose.

        When the source ends normally the tail is flushed and **yielded** — someone
        stopping mid-sentence should still get that sentence translated, and an earlier
        version of this method stored that audio without ever handing it over, which
        retained it for ten seconds to no purpose.

        Every other exit — an exception, or a caller that stops consuming — goes through
        `finally` and **discards** the buffer instead. Nobody is waiting for that audio,
        so storing it would retain content no one asked for. Either way the segmenter ends
        empty and the device is released.
        """
        consecutive_invalid = 0
        try:
            for frame in self._source.frames():
                self._frames_read += 1
                self._audio_bytes_seen += len(frame)

                try:
                    utterance = self._segmenter.push(frame)
                except ValueError:
                    # A malformed buffer is an expected failure from real hardware, not a
                    # programming error (handbook 16). The frame is dropped, not guessed at.
                    self._frames_invalid += 1
                    consecutive_invalid += 1
                    if consecutive_invalid >= self._max_consecutive_invalid:
                        raise CaptureError(
                            f"capture source produced {consecutive_invalid} malformed "
                            "frames in a row; treating the device as failed"
                        ) from None
                    continue

                consecutive_invalid = 0
                if utterance is not None:
                    self._utterances_emitted += 1
                    yield utterance

            # The source ended of its own accord, so someone is still listening: emit the
            # tail rather than dropping a half-finished sentence.
            final = self._segmenter.flush()
            if final is not None:
                self._utterances_emitted += 1
                yield final
        finally:
            self._finish()

    def _finish(self) -> None:
        """Drop any buffered audio and release the device. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            # Anything still buffered here belongs to an abandoned session. It is
            # discarded rather than stored: retaining audio nobody will read is exactly
            # the accidental persistence Article 6 calls out.
            self._segmenter.discard()
        finally:
            # Closing the device runs even if the discard fails. Leaving a microphone open
            # after a session ends is both a privacy problem and a resource leak.
            self._source.close()

    def close(self) -> None:
        """Stop the session early. Safe to call more than once."""
        self._finish()

    def __enter__(self) -> CaptureSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
