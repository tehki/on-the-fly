"""Audio format description and the arithmetic that depends on it.

Frame sizes, durations and byte counts are derived from one validated `AudioFormat` rather
than recomputed with magic numbers at each call site. Getting this wrong is quiet: a frame
size that disagrees with the sample rate produces audio that still plays and translates
badly, with nothing in the logs to say why.
"""

from __future__ import annotations

from dataclasses import dataclass

# 16-bit signed PCM. The only width this pipeline handles today; anything else is rejected
# at construction rather than misinterpreted as silence or noise.
SUPPORTED_SAMPLE_WIDTH_BYTES = 2

# Speech recognition models overwhelmingly expect 16 kHz mono. Higher rates cost CPU and
# memory for no accuracy gain on speech, which matters on the CPU-only baseline in
# docs/PERFORMANCE_BUDGET.md.
RECOMMENDED_SAMPLE_RATE_HZ = 16_000

# Dividing an int16 sample by this maps the type's whole range into [-1, 1), which is the
# float form every model in this project takes. The most negative sample, -32768, becomes
# exactly -1.0; the most positive, 32767, lands just short of 1.0.
#
# This is deliberately *not* `levels.FULL_SCALE`, which is 32767.0 and one count away.
# Both are right for their own question. Normalising audio asks "where in the type's range
# is this sample", and the range is 65536 counts wide. Metering asks "how close is this to
# the loudest thing representable", and the answer for a maximum positive sample should be
# 1.0 rather than 0.99997.
#
# They are written down together because a number that differs by one from another with a
# similar name looks like a typo, and tidying either into the other would be a quiet
# correctness change in whichever it touched.
INT16_NORMALISATION_SCALE = 32768.0


@dataclass(frozen=True)
class AudioFormat:
    """A validated PCM audio format."""

    sample_rate_hz: int = RECOMMENDED_SAMPLE_RATE_HZ
    channels: int = 1
    sample_width_bytes: int = SUPPORTED_SAMPLE_WIDTH_BYTES

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be positive, got {self.sample_rate_hz}")
        if self.channels != 1:
            # Mono only, deliberately. Downmixing is a real decision with real quality
            # consequences, and silently averaging channels here would hide it.
            raise ValueError(
                f"only mono capture is supported, got {self.channels} channels; "
                "downmix in the capture adapter where the choice is visible"
            )
        if self.sample_width_bytes != SUPPORTED_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                f"only {SUPPORTED_SAMPLE_WIDTH_BYTES}-byte (16-bit) samples are supported, "
                f"got {self.sample_width_bytes}"
            )

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate_hz * self.channels * self.sample_width_bytes

    def frame_bytes(self, milliseconds: int) -> int:
        """Bytes in a frame of the given duration.

        Raises when the duration does not land on a whole number of samples, because a
        partial sample would shift every subsequent sample by one byte and turn speech
        into noise.

        **Samples, not bytes.** This counted bytes until 2026-09-10, which is the same
        question only while the sample count is even. At 11.025 kHz a 20 ms frame is 220.5
        samples — 441 bytes, a whole number, and half a sample. `WavFileSource` then read 220
        samples, found 440 bytes where it expected 441, and stopped: **a file full of speech
        produced no audio at all and no error**. The check said "a whole number of samples"
        and asked about bytes.
        """
        if milliseconds <= 0:
            raise ValueError(f"frame duration must be positive, got {milliseconds}ms")
        samples = self.sample_rate_hz * milliseconds
        if samples % 1000 != 0:
            raise ValueError(
                f"a {milliseconds}ms frame is not a whole number of samples at "
                f"{self.sample_rate_hz}Hz; {self._workable_frame_ms()}"
            )
        return samples // 1000 * self.channels * self.sample_width_bytes

    def _workable_frame_ms(self) -> str:
        """A duration that does divide, so the message names a way out rather than a rule.

        Searched rather than derived: the smallest whole millisecond up to 200 ms that lands
        on a whole sample. Every rate consumer hardware produces has one well below that.
        """
        for candidate in range(1, 201):
            if (self.sample_rate_hz * candidate) % 1000 == 0:
                return f"try --frame-ms {candidate}"
        return "choose a duration that divides evenly"  # pragma: no cover - no such rate

    def duration_seconds(self, byte_count: int) -> float:
        """How long `byte_count` bytes of this format last."""
        if byte_count < 0:
            raise ValueError("byte_count cannot be negative")
        return byte_count / self.bytes_per_second

    def validate_frame(self, frame: bytes) -> None:
        """Reject a frame that is not a whole number of samples.

        Called on every frame from a capture device. Device buffers are untrusted input
        like any other (Article 4), and a truncated final buffer is a normal thing for a
        sound card to hand over at the end of a stream.
        """
        if len(frame) % self.sample_width_bytes != 0:
            raise ValueError(
                f"frame of {len(frame)} bytes is not a whole number of "
                f"{self.sample_width_bytes}-byte samples"
            )
