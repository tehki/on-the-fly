"""Any `AudioSource` at the rate the models take, when a caller asks for it (ADR 0046).

`WavFileSource` deliberately refuses to resample: *"doing it invisibly inside a file reader
is how a model ends up being fed something nobody chose. The caller decides."* That refusal
is right and this is the caller deciding — a wrapper, opted into by name, that puts a source
through the same libswresample path the microphone has used since ADR 0013.

The alternative for a user with a 44.1 kHz recording is to leave and come back with `ffmpeg`,
which is a strange thing for an application that already resamples every microphone it opens.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from on_the_fly.domain.audio import AudioFormat
from on_the_fly.infrastructure.audio.resampling import Resampler
from on_the_fly.infrastructure.audio.wav_source import DEFAULT_FRAME_MS

# What every pinned recogniser takes. Named here rather than imported from the ASR package so
# that `infrastructure/audio/` keeps depending on nothing above it.
TARGET_RATE_HZ = 16_000


class ResampledSource:
    """Wraps a source, yielding whole frames at `target_rate_hz`.

    Frame *duration* is preserved rather than frame size: 20 ms of 44.1 kHz audio is 1764
    bytes and 20 ms of 16 kHz audio is 640, so a caller downstream sees the cadence it
    expects. `Resampler` holds at most one input block plus one output frame, so a long
    recording does not become a memory decision here either.
    """

    __slots__ = ("_frame_ms", "_resampler", "_source", "_target_rate")

    def __init__(
        self,
        source: object,
        *,
        target_rate_hz: int = TARGET_RATE_HZ,
        frame_ms: int = DEFAULT_FRAME_MS,
    ) -> None:
        original = source.audio_format  # type: ignore[attr-defined]
        if original.channels != 1:
            raise ValueError(
                f"this resamples mono audio and was given {original.channels} channels. "
                "Mixing channels is a decision about which voice to keep, not a conversion."
            )
        self._source = source
        self._target_rate = target_rate_hz
        self._frame_ms = frame_ms
        self._resampler = Resampler(
            source_rate_hz=original.sample_rate_hz,
            target_rate_hz=target_rate_hz,
            frame_bytes=AudioFormat(sample_rate_hz=target_rate_hz).frame_bytes(self._frame_ms),
        )

    @property
    def audio_format(self) -> AudioFormat:
        """What comes *out*, which is the only rate anything downstream should see."""
        return AudioFormat(sample_rate_hz=self._target_rate)

    @property
    def source_rate_hz(self) -> int:
        """What went in. Printed, so a run says what it converted rather than hiding it."""
        return self._resampler.source_rate_hz

    @property
    def path(self) -> Path:
        """The file underneath, so a report names the recording rather than the wrapper."""
        return self._source.path  # type: ignore[attr-defined,no-any-return]

    @property
    def declared_seconds(self) -> float:
        """The header's claim, in the original file's terms.

        Deliberately not rescaled: a truncation check compares what a file said about itself
        with what it handed over, and both of those are facts about the file rather than
        about this wrapper (ADR 0046).
        """
        return self._source.declared_seconds  # type: ignore[attr-defined,no-any-return]

    @property
    def delivered_seconds(self) -> float:
        return self._source.delivered_seconds  # type: ignore[attr-defined,no-any-return]

    @property
    def truncated_seconds(self) -> float:
        return self._source.truncated_seconds  # type: ignore[attr-defined,no-any-return]

    @property
    def is_truncated(self) -> bool:
        return self._source.is_truncated  # type: ignore[attr-defined,no-any-return]

    def frames(self) -> Generator[bytes]:
        for block in self._source.frames():  # type: ignore[attr-defined]
            yield from self._resampler.push(block)

    def close(self) -> None:
        self._resampler.reset()
        self._source.close()  # type: ignore[attr-defined]

    def __enter__(self) -> ResampledSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
