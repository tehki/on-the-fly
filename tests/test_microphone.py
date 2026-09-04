"""Tests for the microphone adapter.

Almost everything here runs against a fake backend, which is the point of having a
`CaptureBackend` port at all: CI has no microphone, and neither does a laptop with the lid
shut. The adapter's real behaviour — when the device is opened, when it is released, how
overflows and failures are handled — is all testable without hardware.

One test does exercise the real `sounddevice` backend, and skips if it is not installed. It
does not capture audio; it checks that a device that cannot be opened produces this
project's error type rather than a library-specific exception leaking upward.
"""

from __future__ import annotations

from array import array

import pytest

from on_the_fly.domain.audio import (
    AudioFormat,
    CaptureSession,
    EndReason,
    EnergyVoiceActivityDetector,
    SegmenterConfig,
)
from on_the_fly.domain.retention import EphemeralStore, ManualClock
from on_the_fly.infrastructure.audio import (
    AudioDeviceError,
    MicrophoneSource,
    SoundDeviceBackend,
)

FORMAT = AudioFormat()
FRAME_MS = 20
FRAME_BYTES = FORMAT.frame_bytes(FRAME_MS)
SAMPLES_PER_FRAME = FRAME_BYTES // FORMAT.sample_width_bytes


def frame_of(value: int) -> bytes:
    return array("h", [value] * SAMPLES_PER_FRAME).tobytes()


SILENT_FRAME = frame_of(0)
LOUD_FRAME = frame_of(8000)


class FakeStream:
    """A capture stream that plays back scripted frames."""

    def __init__(
        self,
        frames: list[bytes],
        *,
        overflow_at: set[int] | None = None,
        fail_at: int | None = None,
    ) -> None:
        self._frames = frames
        self._overflow_at = overflow_at or set()
        self._fail_at = fail_at
        self.index = 0
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def read(self, frames: int) -> tuple[bytes, bool]:
        if self._fail_at is not None and self.index >= self._fail_at:
            raise AudioDeviceError("device disconnected")
        if self.index >= len(self._frames):
            return b"", False
        frame = self._frames[self.index]
        overflowed = self.index in self._overflow_at
        self.index += 1
        return frame, overflowed

    def close(self) -> None:
        self.closed += 1


class FakeBackend:
    """Records how it was opened, so the adapter's negotiation can be asserted."""

    def __init__(
        self,
        stream: FakeStream | None = None,
        *,
        fail_to_open: bool = False,
        supported_rates: set[int] | None = None,
        native_rate: int | None = None,
    ) -> None:
        self.stream = stream if stream is not None else FakeStream([])
        self.fail_to_open = fail_to_open
        # None means "accepts whatever it is asked for", which is what a fake did before
        # rate negotiation existed and keeps every older test meaningful.
        self.supported_rates = supported_rates
        self.native_rate = native_rate
        self.open_calls: list[dict[str, object]] = []
        self.refused_rates: list[int] = []
        self.probed_rates: list[int] = []

    @property
    def name(self) -> str:
        return "fake"

    def default_sample_rate(self, device: int | str | None = None) -> int | None:
        return self.native_rate

    def supports_rate(
        self, sample_rate_hz: int, *, channels: int, device: int | str | None = None
    ) -> bool:
        self.probed_rates.append(sample_rate_hz)
        if self.supported_rates is None:
            return True
        return sample_rate_hz in self.supported_rates

    def open_input_stream(
        self,
        *,
        sample_rate_hz: int,
        channels: int,
        blocksize: int,
        device: int | str | None = None,
    ) -> FakeStream:
        self.open_calls.append(
            {
                "sample_rate_hz": sample_rate_hz,
                "channels": channels,
                "blocksize": blocksize,
                "device": device,
            }
        )
        if self.fail_to_open:
            raise AudioDeviceError("no such device")
        if self.supported_rates is not None and sample_rate_hz not in self.supported_rates:
            self.refused_rates.append(sample_rate_hz)
            raise AudioDeviceError(
                f"could not open an input stream at {sample_rate_hz}Hz: Error opening Raw"
            )
        return self.stream


# ======================================================================================
# Opening and releasing the device
# ======================================================================================


def test_constructing_a_source_opens_no_device() -> None:
    """An application can build one at startup without lighting up the microphone."""
    backend = FakeBackend()
    source = MicrophoneSource(backend=backend)

    assert backend.open_calls == []
    assert source.is_open is False


def test_the_device_is_opened_with_the_negotiated_format() -> None:
    backend = FakeBackend(FakeStream([SILENT_FRAME]))
    source = MicrophoneSource(backend=backend, frame_ms=FRAME_MS, device=3)

    list(source.frames())

    assert len(backend.open_calls) == 1
    call = backend.open_calls[0]
    assert call["sample_rate_hz"] == FORMAT.sample_rate_hz
    assert call["channels"] == 1
    assert call["blocksize"] == SAMPLES_PER_FRAME
    assert call["device"] == 3


def test_frames_are_yielded_and_counted() -> None:
    stream = FakeStream([SILENT_FRAME, LOUD_FRAME, SILENT_FRAME])
    source = MicrophoneSource(backend=FakeBackend(stream))

    collected = list(source.frames())

    assert collected == [SILENT_FRAME, LOUD_FRAME, SILENT_FRAME]
    assert source.frames_yielded == 3
    assert stream.started == 1
    assert stream.closed == 1


def test_the_device_is_released_when_the_stream_ends() -> None:
    stream = FakeStream([SILENT_FRAME])
    source = MicrophoneSource(backend=FakeBackend(stream))

    list(source.frames())

    assert stream.closed == 1
    assert source.is_open is False


def test_the_device_is_released_when_the_caller_stops_early() -> None:
    """A microphone left open by an abandoned loop is a privacy problem, not just a leak."""
    stream = FakeStream([SILENT_FRAME] * 100)
    source = MicrophoneSource(backend=FakeBackend(stream))

    with source:
        for _ in source.frames():
            break

    assert stream.closed >= 1
    assert source.is_open is False


def test_the_device_is_released_when_the_generator_is_closed() -> None:
    stream = FakeStream([SILENT_FRAME] * 100)
    source = MicrophoneSource(backend=FakeBackend(stream))

    generator = source.frames()
    next(generator)
    generator.close()

    assert stream.closed >= 1


def test_the_device_is_released_when_a_read_fails() -> None:
    stream = FakeStream([SILENT_FRAME, SILENT_FRAME], fail_at=2)
    source = MicrophoneSource(backend=FakeBackend(stream))

    with pytest.raises(AudioDeviceError, match="disconnected"):
        list(source.frames())

    assert stream.closed == 1, "the device must be released on the failure path too"


def test_close_is_idempotent() -> None:
    stream = FakeStream([SILENT_FRAME])
    source = MicrophoneSource(backend=FakeBackend(stream))
    list(source.frames())

    source.close()
    source.close()

    assert stream.closed == 1


def test_a_closed_source_refuses_to_capture_again() -> None:
    source = MicrophoneSource(backend=FakeBackend())
    source.close()

    with pytest.raises(AudioDeviceError, match="closed"):
        list(source.frames())


def test_capturing_twice_at_once_is_refused() -> None:
    source = MicrophoneSource(backend=FakeBackend(FakeStream([SILENT_FRAME] * 10)))
    first = source.frames()
    next(first)

    with pytest.raises(AudioDeviceError, match="already capturing"):
        list(source.frames())

    first.close()


def test_a_device_that_cannot_be_opened_raises_the_project_error_type() -> None:
    source = MicrophoneSource(backend=FakeBackend(fail_to_open=True))

    with pytest.raises(AudioDeviceError, match="no such device"):
        list(source.frames())


# ======================================================================================
# Reporting
# ======================================================================================


def test_overflows_are_counted_rather_than_hidden() -> None:
    """An overflow is a dropped word. It is surfaced so the budget can see it."""
    stream = FakeStream([SILENT_FRAME] * 5, overflow_at={1, 3})
    source = MicrophoneSource(backend=FakeBackend(stream))

    collected = list(source.frames())

    assert len(collected) == 5, "an overflow does not lose the frame that was read"
    assert source.overflow_count == 2


def test_repr_names_no_device() -> None:
    """A device name can identify a person, and a repr ends up in bug reports."""
    source = MicrophoneSource(backend=FakeBackend(), device="Ilya's AirPods")

    rendered = repr(source)

    assert "AirPods" not in rendered
    assert "fake" in rendered


def test_a_frame_size_that_is_not_whole_samples_is_refused_before_any_device_opens() -> None:
    backend = FakeBackend()
    with pytest.raises(ValueError, match="whole number of samples"):
        MicrophoneSource(
            audio_format=AudioFormat(sample_rate_hz=44_100), frame_ms=1, backend=backend
        )
    assert backend.open_calls == [], "validation must happen before touching hardware"


# ======================================================================================
# End to end through the pipeline
# ======================================================================================


def test_microphone_through_the_pipeline_into_retention() -> None:
    """The first time capture, segmentation and retention run as one thing."""
    clock = ManualClock()
    store = EphemeralStore("on-the-fly", clock=clock)
    frames = [SILENT_FRAME] * 3 + [LOUD_FRAME] * 4 + [SILENT_FRAME] * 4
    source = MicrophoneSource(backend=FakeBackend(FakeStream(frames)))
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=40,
        hangover_ms=40,
        min_utterance_ms=20,
        max_utterance_ms=1000,
    )

    session = CaptureSession(
        source=source,
        detector=EnergyVoiceActivityDetector(),
        store=store,
        config=config,
    )
    utterances = list(session.utterances())

    assert len(utterances) == 1, "one burst of speech should produce one utterance"
    utterance = utterances[0]
    assert utterance.ended_because is EndReason.SILENCE
    assert store.is_present(utterance.handle)

    with store.borrow(utterance.handle) as audio:
        assert isinstance(audio, bytes)
        assert LOUD_FRAME in audio, "the speech itself should be in the stored utterance"

    # And the retention rule still governs it.
    clock.advance(10.001)
    store.reap()
    assert not store.is_present(utterance.handle)
    assert source.is_open is False, "the session must release the microphone"


# ======================================================================================
# The real backend
# ======================================================================================


def test_real_backend_maps_an_unopenable_device_to_the_project_error() -> None:
    """Exercises the actual sounddevice binding, without capturing anything.

    Skips when the runtime dependency is absent, so the domain test suite runs with no
    audio library installed. CI installs it, along with libportaudio2, so this runs on
    every pull request.
    """
    pytest.importorskip("sounddevice")

    backend = SoundDeviceBackend()
    assert backend.name == "sounddevice"

    with pytest.raises(AudioDeviceError):
        backend.open_input_stream(sample_rate_hz=16_000, channels=1, blocksize=320, device=999_999)


def test_real_backend_can_enumerate_input_devices() -> None:
    """A headless runner legitimately has none; an empty list is a valid answer."""
    pytest.importorskip("sounddevice")

    names = SoundDeviceBackend().input_device_names()

    assert isinstance(names, tuple)
    assert all(isinstance(name, str) for name in names)


# ======================================================================================
# Capture rate negotiation (ADR 0013)
#
# Measured on real hardware: both analog inputs refuse 16 kHz and offer 44.1/48 kHz. The
# adapter therefore asks rather than demands. What the domain sees must not change.
# ======================================================================================


def test_a_device_offering_the_wanted_rate_is_not_resampled() -> None:
    """The common case must stay byte-for-byte what it was."""
    backend = FakeBackend(FakeStream([b"\x01\x02" * 320]), supported_rates={16000})
    source = MicrophoneSource(backend=backend, frame_ms=20)

    frames = list(source.frames())

    assert source.capture_rate_hz == 16000
    assert source.is_resampling is False
    assert frames == [b"\x01\x02" * 320]


def test_a_device_refusing_the_wanted_rate_is_opened_at_its_native_rate() -> None:
    """Measured behaviour of the reference machine's built-in input: 48 kHz only."""
    backend = FakeBackend(FakeStream([bytes(2 * 960)]), supported_rates={48000}, native_rate=48000)
    source = MicrophoneSource(backend=backend, frame_ms=20)

    list(source.frames())

    assert backend.probed_rates[0] == 16000, "the wanted rate is asked for first"
    assert source.capture_rate_hz == 48000
    assert source.is_resampling is True


def test_the_native_rate_is_tried_before_the_candidate_list() -> None:
    """A device that can give us what it prefers should not be asked to try 48 kHz first."""
    backend = FakeBackend(FakeStream([bytes(2 * 882)]), supported_rates={44100}, native_rate=44100)
    source = MicrophoneSource(backend=backend, frame_ms=20)

    list(source.frames())

    # Probed in order, opened exactly once: repeated failed opens crash PortAudio.
    assert backend.probed_rates[:2] == [16000, 44100]
    assert [call["sample_rate_hz"] for call in backend.open_calls] == [44100]


def test_the_read_block_covers_the_same_duration_at_the_negotiated_rate() -> None:
    """20 ms is 320 samples at 16 kHz and 960 at 48 kHz. The cadence must not change."""
    backend = FakeBackend(FakeStream([bytes(2 * 960)]), supported_rates={48000}, native_rate=48000)
    source = MicrophoneSource(backend=backend, frame_ms=20)

    list(source.frames())

    assert backend.open_calls[-1]["blocksize"] == 960


def test_a_device_supporting_nothing_still_gets_exactly_one_open_attempt() -> None:
    """Probing may be unreliable, so one honest attempt is made. Only one: retrying a
    failed open is what corrupts PortAudio's heap (ADR 0013)."""
    backend = FakeBackend(supported_rates=set(), native_rate=None)
    source = MicrophoneSource(backend=backend)

    with pytest.raises(AudioDeviceError, match="could not open the device at 16000Hz"):
        list(source.frames())

    assert len(backend.open_calls) == 1, "a failed open must never be retried"
    assert len(backend.probed_rates) > 1, "the fallback list is probed, not opened"


def test_the_underlying_reason_survives_negotiation() -> None:
    """A missing device is not a rate problem, and must not be reported as one."""
    backend = FakeBackend(fail_to_open=True)
    source = MicrophoneSource(backend=backend)

    with pytest.raises(AudioDeviceError, match="no such device"):
        list(source.frames())


def test_the_negotiated_rate_is_reported_without_a_device_name() -> None:
    """A rate is OPERATIONAL_METADATA. A device name identifies a person (ADR 0003)."""
    backend = FakeBackend(FakeStream([bytes(2 * 960)]), supported_rates={48000}, native_rate=48000)
    source = MicrophoneSource(backend=backend)

    list(source.frames())
    rendered = repr(source)

    assert "capture_rate=48000" in rendered
    assert "fake" in rendered
