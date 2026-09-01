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
        """
        if milliseconds <= 0:
            raise ValueError(f"frame duration must be positive, got {milliseconds}ms")
        exact = self.bytes_per_second * milliseconds
        if exact % 1000 != 0:
            raise ValueError(
                f"a {milliseconds}ms frame is not a whole number of samples at "
                f"{self.sample_rate_hz}Hz; choose a duration that divides evenly"
            )
        return exact // 1000

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
