"""Voice activity detection, energy-based and dependency-free.

This is a real detector, not a stub: it tracks a noise floor and adapts, which is enough to
segment speech in a reasonably quiet room. It is also honestly limited — energy alone
cannot distinguish a voice from a slammed door, and it will cut on a loud non-speech sound.
Silero VAD (MIT) is the intended replacement and is a dependency-admission decision under
Article 12, made separately.

It is written to be replaceable rather than to be clever. `VoiceActivityDetector` in
`ports.py` is the seam; all the timing decisions live in the segmenter, so swapping this
for a neural detector changes accuracy and nothing else.

No `audioop`: it was removed in Python 3.13. RMS is computed over `array` instead, which is
stdlib, correct, and fast enough for 20ms frames.
"""

from __future__ import annotations

import math
from array import array

# Below this RMS, a frame is treated as silence regardless of the adaptive floor. Stops a
# very quiet room from adapting its noise floor down to near zero and then hearing speech
# in the dither of an idle microphone.
DEFAULT_ABSOLUTE_SILENCE_RMS = 120.0

# How far above the noise floor a frame must sit to count as speech.
DEFAULT_SPEECH_FACTOR = 3.0

# Per-frame weight for noise-floor adaptation. Small, so the floor tracks room tone over
# seconds rather than being dragged upward by the speech it is supposed to detect.
DEFAULT_ADAPTATION_RATE = 0.05


def frame_rms(frame: bytes) -> float:
    """Root-mean-square amplitude of a 16-bit PCM frame.

    An empty frame has an RMS of zero rather than raising: a device legitimately hands
    over a zero-length buffer when a stream stops.
    """
    if not frame:
        return 0.0
    if len(frame) % 2 != 0:
        raise ValueError(f"frame of {len(frame)} bytes is not a whole number of 16-bit samples")
    samples = array("h")
    samples.frombytes(frame)
    # sys.byteorder handling is the capture adapter's job; PCM from a device is
    # native-endian, and array reads it the same way.
    total = math.fsum(float(sample) * float(sample) for sample in samples)
    return math.sqrt(total / len(samples))


class EnergyVoiceActivityDetector:
    """Speech when a frame is meaningfully louder than the adapted noise floor."""

    __slots__ = (
        "_absolute_silence_rms",
        "_adaptation_rate",
        "_noise_floor",
        "_seeded",
        "_speech_factor",
    )

    def __init__(
        self,
        *,
        absolute_silence_rms: float = DEFAULT_ABSOLUTE_SILENCE_RMS,
        speech_factor: float = DEFAULT_SPEECH_FACTOR,
        adaptation_rate: float = DEFAULT_ADAPTATION_RATE,
    ) -> None:
        if absolute_silence_rms < 0:
            raise ValueError("absolute_silence_rms cannot be negative")
        if speech_factor <= 1.0:
            raise ValueError("speech_factor must exceed 1.0, otherwise room tone counts as speech")
        if not 0.0 < adaptation_rate < 1.0:
            raise ValueError("adaptation_rate must be between 0 and 1, exclusive")

        self._absolute_silence_rms = absolute_silence_rms
        self._speech_factor = speech_factor
        self._adaptation_rate = adaptation_rate
        self._noise_floor = 0.0
        self._seeded = False

    @property
    def noise_floor(self) -> float:
        """Current adapted noise floor, for diagnostics. Carries no content."""
        return self._noise_floor

    def reset(self) -> None:
        self._noise_floor = 0.0
        self._seeded = False

    def is_speech(self, frame: bytes) -> bool:
        rms = frame_rms(frame)

        if not self._seeded:
            # The first frame defines the starting floor. Without this the detector spends
            # its first second adapting upward from zero and reports speech throughout.
            self._noise_floor = rms
            self._seeded = True

        threshold = max(self._noise_floor * self._speech_factor, self._absolute_silence_rms)
        speech = rms > threshold

        if not speech:
            # Adapt only on silence. Adapting on speech would raise the floor until the
            # speaker stopped being audible to the detector — quietly, and worse the
            # longer someone talks.
            self._noise_floor += self._adaptation_rate * (rms - self._noise_floor)

        return speech
