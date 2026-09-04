"""Resampling captured audio to the rate the pipeline works in (ADR 0013).

Real capture hardware offers 44.1 kHz and 48 kHz; the recognition models take 16 kHz. When
a device refuses the rate we want, `MicrophoneSource` opens at one the device accepts and
puts the audio through here on the way out. Above the adapter nothing changes: the domain
still receives 16 kHz mono int16 frames of a fixed size.

**Why libswresample rather than arithmetic.** Downsampling without an anti-aliasing filter
folds high-frequency content back into the speech band. It raises no error — it degrades
recognition quietly, which is the failure mode this project has already been caught by once
(ADR 0009). 48 kHz to 16 kHz is a clean 3:1 decimation, but 44.1 kHz to 16 kHz is 160:441,
which needs a designed filter. `av` wraps the resampler FFmpeg uses, and it was already an
installed requirement of `faster-whisper` before this module existed.

**The buffer is bounded.** Resampling does not preserve block sizes, so output is
accumulated until a whole frame is available. At most one input block plus one output frame
is held, and `reset()` discards it. Resampled audio is `EPHEMERAL` like the audio it came
from, and nothing here writes it anywhere.
"""

from __future__ import annotations

from typing import Any

from on_the_fly.infrastructure.audio.backend import AudioDeviceError

# Rates to try when the device refuses the one we want, in preference order. The device's
# own native rate is tried before any of these; this is the fallback list, and it is the two
# rates consumer audio hardware actually offers.
CANDIDATE_RATES: tuple[int, ...] = (48000, 44100, 32000, 22050)


class Resampler:
    """Converts mono int16 PCM from one sample rate to another, preserving frame size.

    Feed it whatever the device produced; take whole frames out. A partial frame stays in
    the buffer until enough audio arrives to complete it, which is what keeps the output
    frame size fixed while the input block size is not.
    """

    def __init__(self, *, source_rate_hz: int, target_rate_hz: int, frame_bytes: int) -> None:
        if source_rate_hz <= 0 or target_rate_hz <= 0:
            raise ValueError("sample rates must be positive")
        if frame_bytes <= 0 or frame_bytes % 2:
            raise ValueError("frame_bytes must be a positive whole number of int16 samples")

        self._source_rate = source_rate_hz
        self._target_rate = target_rate_hz
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()
        self._resampler: Any | None = None
        self._format: Any | None = None

    @property
    def source_rate_hz(self) -> int:
        return self._source_rate

    @property
    def target_rate_hz(self) -> int:
        return self._target_rate

    @property
    def pending_bytes(self) -> int:
        """Audio held back because it does not yet complete a frame."""
        return len(self._buffer)

    def __repr__(self) -> str:
        return (
            f"Resampler({self._source_rate}->{self._target_rate}, "
            f"frame_bytes={self._frame_bytes}, pending={len(self._buffer)})"
        )

    def _ensure_resampler(self) -> tuple[Any, Any]:
        if self._resampler is not None and self._format is not None:
            return self._resampler, self._format
        try:
            import av
        except ImportError as exc:  # pragma: no cover - exercised by the requirements install
            raise AudioDeviceError(
                "av is required to resample captured audio; install the runtime "
                f"requirements. Underlying error: {exc}"
            ) from exc

        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self._target_rate)
        self._format = av
        return self._resampler, av

    def push(self, pcm: bytes) -> list[bytes]:
        """Resample one block and return whatever whole frames that completed.

        Returns an empty list when the block was too short to finish a frame; that is
        normal and not an error.
        """
        if not pcm:
            return []

        resampler, av = self._ensure_resampler()
        try:
            import numpy as np

            samples = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate = self._source_rate
            for converted in resampler.resample(frame):
                self._buffer.extend(bytes(converted.planes[0]))
        except AudioDeviceError:
            raise
        except Exception as exc:
            # The resampler's own exceptions are not this module's contract and may carry
            # buffer contents. Translate at the boundary without echoing audio.
            raise AudioDeviceError(
                f"could not resample {self._source_rate}Hz capture to "
                f"{self._target_rate}Hz: {type(exc).__name__}"
            ) from exc

        return self._drain()

    def _drain(self) -> list[bytes]:
        frames: list[bytes] = []
        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]
        return frames

    def reset(self) -> None:
        """Discard buffered audio and resampler state.

        Called when the device closes. A partial frame is dropped rather than padded:
        inventing silence to complete it would hand the recogniser audio nobody spoke.
        """
        self._buffer.clear()
        self._resampler = None
