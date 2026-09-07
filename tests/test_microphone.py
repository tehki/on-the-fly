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

import queue as _queue
import sys
import types
from array import array
from collections.abc import Callable
from typing import ClassVar, cast

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
from on_the_fly.infrastructure.audio.backend import InputStream, _SoundDeviceStream

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


# --------------------------------------------------------------------------------------
# What comes out of the resampled path, not just how it was opened.
#
# Every test above this point that touches resampling feeds silence and asserts the
# blocksize that was requested. None looked at the audio, and a resampler that emitted
# 1.19x its input — the surplus being stale samples from earlier blocks — passed all of
# them. It reached the recogniser as `THE SQUALID QUARTER OF THE BROTHELS` becoming
# `WHILE ITS WATER AT THE BOTTOM`, at 38.9% word error.
#
# The unit-level guard lives in `test_resampling.py`. These are here because this is the
# layer the defect actually shipped in: a device that refuses 16 kHz, seen through the
# adapter the application uses.
# --------------------------------------------------------------------------------------


def tone_at(frequency_hz: float, samples: int, rate_hz: int) -> bytes:
    """A sine wave as mono int16, for asserting that audio survives a conversion."""
    import math

    values = array("h")
    for index in range(samples):
        values.append(int(8000 * math.sin(2 * math.pi * frequency_hz * index / rate_hz)))
    return values.tobytes()


def dominant_frequency_of(pcm: bytes, rate_hz: int) -> float:
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(len(samples), 1.0 / rate_hz)[int(np.argmax(spectrum))])


def test_a_resampled_device_yields_the_duration_it_was_given() -> None:
    """Fifty 20 ms blocks at 48 kHz are one second, and must not become 1.19 seconds.

    This is the assertion whose absence let the resampler emit padding as audio. A frame
    count is the cheapest possible check and nothing was making it.
    """
    blocks = 50
    stream = FakeStream([tone_at(440.0, 960, 48000) for _ in range(blocks)])
    backend = FakeBackend(stream, supported_rates={48000}, native_rate=48000)
    source = MicrophoneSource(backend=backend, frame_ms=FRAME_MS)

    frames = list(source.frames())

    assert source.is_resampling is True
    # Never more than went in: a surplus is invented audio. A little less is the
    # resampler's own filter delay, still inside it when the stream ends.
    assert len(frames) <= blocks
    assert len(frames) >= blocks - 2
    assert all(len(frame) == FRAME_BYTES for frame in frames)


def test_audio_survives_the_resampled_capture_path() -> None:
    """440 Hz into a 48 kHz device is 440 Hz out of the adapter.

    Padding does not merely add duration — it interleaves stale audio with live audio. A
    frame count alone would not notice that, so the content is checked too.
    """
    stream = FakeStream([tone_at(440.0, 960, 48000) for _ in range(100)])
    backend = FakeBackend(stream, supported_rates={48000}, native_rate=48000)
    source = MicrophoneSource(backend=backend, frame_ms=FRAME_MS)

    captured = b"".join(source.frames())

    assert captured
    assert abs(dominant_frequency_of(captured, 16000) - 440.0) < 25.0


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


# ======================================================================================
# The backend seam itself
#
# This file's own docstring says the port exists so "the adapter's real behaviour ... is all
# testable without hardware". The adapter is. The `sounddevice` backend behind it was not:
# mutation testing killed **none** of its thirty sites.
#
# It needs no hardware either. `_SoundDeviceStream` takes its stream and its queue as
# arguments, and `SoundDeviceBackend` imports `sounddevice` lazily inside each method — so a
# fake module in `sys.modules` is enough. What is under test is the code that decides "audio
# was lost" and "the device is gone", which is what the command line prints.
# ======================================================================================


class RawStream:
    """Stands in for a PortAudio stream. Optionally refuses to start, stop or close."""

    def __init__(self, *, fail_start: bool = False, fail_close: bool = False) -> None:
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self._fail_start = fail_start
        self._fail_close = fail_close

    def start(self) -> None:
        if self._fail_start:
            raise RuntimeError("device busy")
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1
        if self._fail_close:
            raise RuntimeError("stop failed")

    def close(self) -> None:
        self.closed += 1
        if self._fail_close:
            raise RuntimeError("close failed")


def stream_over(blocks: list[bytes], **kwargs: object) -> tuple[_SoundDeviceStream, RawStream]:
    queued: _queue.Queue[bytes] = _queue.Queue(maxsize=_SoundDeviceStream.MAX_BLOCKS)
    for block in blocks:
        queued.put(block)
    raw = RawStream(**kwargs)  # type: ignore[arg-type]
    return _SoundDeviceStream(raw, queued), raw


# --- starting and stopping ----------------------------------------------------------------


def test_starting_twice_starts_the_device_once() -> None:
    stream, raw = stream_over([])
    stream.start()
    stream.start()

    assert raw.started == 1


def test_a_device_that_refuses_to_start_becomes_this_projects_error() -> None:
    """A library exception leaking upward is what the port exists to prevent."""
    stream, _ = stream_over([], fail_start=True)

    with pytest.raises(AudioDeviceError, match="could not start"):
        stream.start()


def test_a_closed_stream_cannot_be_started_or_read() -> None:
    stream, _ = stream_over([SILENT_FRAME])
    stream.close()

    with pytest.raises(AudioDeviceError, match="has been closed"):
        stream.start()
    with pytest.raises(AudioDeviceError, match="has been closed"):
        stream.read(160)


def test_closing_twice_stops_the_device_once() -> None:
    stream, raw = stream_over([])
    stream.close()
    stream.close()

    assert (raw.stopped, raw.closed) == (1, 1)


def test_closing_does_not_raise_even_when_the_device_does() -> None:
    """A failure here would mask whatever error caused the shutdown, and the caller can do
    nothing useful about it either way."""
    stream, raw = stream_over([SILENT_FRAME], fail_close=True)

    stream.close()

    assert raw.closed == 1


def test_closing_releases_audio_still_queued() -> None:
    """Captured audio is EPHEMERAL and has no reason to outlive the device that made it."""
    stream, _ = stream_over([SILENT_FRAME] * 5)

    stream.close()

    assert stream._queue.empty(), "captured audio outlived the device"


# --- what "audio was lost" means ------------------------------------------------------------


def test_a_read_reports_no_overflow_when_nothing_was_dropped() -> None:
    stream, _ = stream_over([LOUD_FRAME])

    data, overflowed = stream.read(160)

    assert data == LOUD_FRAME
    assert not overflowed


def test_a_dropped_block_is_reported_on_the_next_read() -> None:
    """This is what the command line prints as "dropped N overflow(s) - audio was lost"."""
    stream, _ = stream_over([LOUD_FRAME])
    stream.note_drop()

    assert stream.read(160)[1]


def test_portaudio_reporting_its_own_overflow_counts_too() -> None:
    """Two independent ways audio goes missing: our queue filling, and the driver saying so."""
    stream, _ = stream_over([LOUD_FRAME])
    stream.note_status_overflow()

    assert stream.read(160)[1]


def test_an_overflow_is_reported_once_and_not_again() -> None:
    """The counters are cleared as they are read. Otherwise one drop early in a session
    would mark every subsequent frame as lost audio, and the warning would stop meaning
    anything."""
    stream, _ = stream_over([LOUD_FRAME, LOUD_FRAME])
    stream.note_drop()
    stream.note_status_overflow()

    assert stream.read(160)[1]
    assert not stream.read(160)[1], "the overflow was reported twice"


def test_a_device_that_stops_delivering_is_reported_as_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five seconds of nothing from a device that claimed to be running is a failure, not a
    quiet room: the callback delivers blocks whether or not anyone is speaking."""
    stream, _ = stream_over([])

    with pytest.raises(AudioDeviceError, match="may have been disconnected"):
        monkeypatch.setattr(stream._queue, "get", _raise_empty)
        stream.read(160)


def _raise_empty(*args: object, **kwargs: object) -> bytes:
    raise _queue.Empty


def test_the_queue_is_bounded_so_a_stalled_consumer_loses_audio_rather_than_memory() -> None:
    """Unbounded buffering would trade a drop-out for a memory leak, and hand the recogniser
    audio that is seconds stale — worse, for a live translator, than losing it."""
    assert _SoundDeviceStream.MAX_BLOCKS == 100, "two seconds at 20 ms blocks"


# --- the lazy import, and what it says when it fails ------------------------------------------


def fake_sounddevice(monkeypatch: pytest.MonkeyPatch, **attributes: object) -> types.ModuleType:
    module = types.ModuleType("sounddevice")
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


def test_a_missing_sounddevice_package_says_to_install_the_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library-specific ImportError leaking upward is what the port exists to prevent.

    The sibling branch, an OSError from a missing libportaudio2, is left to the one test in
    this file that exercises the real backend: reaching it here would mean patching
    `builtins.__import__`, which is a fragile way to assert a message.
    """
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(AudioDeviceError, match="not installed"):
        SoundDeviceBackend()._import_sounddevice()


def test_a_native_rate_that_cannot_be_read_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not knowing the native rate is a reason to try the candidate list, not a reason to
    fail before the device has been asked for anything."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise RuntimeError("no such device")

    fake_sounddevice(monkeypatch, query_devices=refuse)

    assert SoundDeviceBackend().default_sample_rate(device=3) is None


def test_a_native_rate_of_zero_is_treated_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device reporting zero is reporting nothing usable, and zero would be passed
    straight into a division."""
    fake_sounddevice(monkeypatch, query_devices=lambda device: {"default_samplerate": 0.0})

    assert SoundDeviceBackend().default_sample_rate(device=3) is None


def test_a_native_rate_is_returned_as_a_whole_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """PortAudio reports it as a float; every rate in this project is an int."""
    fake_sounddevice(monkeypatch, query_devices=lambda device: {"default_samplerate": 44100.0})

    assert SoundDeviceBackend().default_sample_rate(device=3) == 44100


def test_an_unsupported_rate_is_false_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`supports_rate` is asked speculatively, once per candidate rate."""

    def refuse(**kwargs: object) -> None:
        raise ValueError("Invalid sample rate")

    fake_sounddevice(monkeypatch, check_input_settings=refuse)

    assert not SoundDeviceBackend().supports_rate(16_000, channels=1)


def test_a_supported_rate_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sounddevice(monkeypatch, check_input_settings=lambda **kwargs: None)

    assert SoundDeviceBackend().supports_rate(48_000, channels=1)


def test_only_devices_that_can_record_are_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A picker listing speakers as microphones would be a picker nobody trusts."""
    fake_sounddevice(
        monkeypatch,
        query_devices=lambda: [
            {"name": "Internal Mic", "max_input_channels": 2},
            {"name": "Speakers", "max_input_channels": 0},
            {"name": "USB Headset", "max_input_channels": 1},
        ],
    )

    assert SoundDeviceBackend().input_device_names() == ("Internal Mic", "USB Headset")


def test_a_device_with_no_name_is_still_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping it would leave the picker's indices disagreeing with the driver's."""
    fake_sounddevice(monkeypatch, query_devices=lambda: [{"max_input_channels": 1}])

    assert SoundDeviceBackend().input_device_names() == ("unknown",)


def test_devices_that_cannot_be_enumerated_become_this_projects_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> object:
        raise RuntimeError("PortAudio not initialised")

    fake_sounddevice(monkeypatch, query_devices=refuse)

    with pytest.raises(AudioDeviceError, match="could not enumerate"):
        SoundDeviceBackend().input_device_names()


# --- the audio callback, which decides which audio is lost -------------------------------
#
# It runs on PortAudio's own thread, so it is written to do nothing but a copy and a put.
# That makes it the one piece of this backend with a real decision in it — when the queue is
# full, which block goes — and it is reachable without a device by capturing the callback
# the backend hands to `RawInputStream`.


class CapturingRawInputStream:
    """Records the callback the backend registers, and does nothing else."""

    registered: ClassVar[dict[str, object]] = {}

    def __init__(self, **kwargs: object) -> None:
        CapturingRawInputStream.registered = dict(kwargs)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


AudioCallback = Callable[[bytes, int, object, object], None]


def opened_stream(monkeypatch: pytest.MonkeyPatch) -> tuple[InputStream, AudioCallback]:
    """`(stream, callback)` for a backend opened against a fake PortAudio."""
    fake_sounddevice(monkeypatch, RawInputStream=CapturingRawInputStream)
    stream = SoundDeviceBackend().open_input_stream(
        sample_rate_hz=16_000, channels=1, blocksize=320
    )
    callback = CapturingRawInputStream.registered["callback"]
    assert callable(callback)
    return stream, cast("AudioCallback", callback)


def test_a_delivered_block_reaches_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, on_audio = opened_stream(monkeypatch)

    on_audio(LOUD_FRAME, 320, None, None)

    assert stream.read(320) == (LOUD_FRAME, False)


def test_a_stream_that_cannot_be_opened_becomes_this_projects_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: object) -> object:
        raise RuntimeError("Invalid number of channels")

    fake_sounddevice(monkeypatch, RawInputStream=refuse)

    with pytest.raises(AudioDeviceError, match="could not open an input stream"):
        SoundDeviceBackend().open_input_stream(sample_rate_hz=16_000, channels=1, blocksize=320)


def test_a_full_queue_loses_the_oldest_block_and_keeps_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a live translator the most recent speech is the useful part, so a consumer that
    has fallen behind loses what it has already missed rather than what is being said now.
    """
    stream, on_audio = opened_stream(monkeypatch)
    oldest = frame_of(1)
    on_audio(oldest, 320, None, None)
    for index in range(2, _SoundDeviceStream.MAX_BLOCKS + 1):
        on_audio(frame_of(index), 320, None, None)

    newest = frame_of(9999)
    on_audio(newest, 320, None, None)

    data, overflowed = stream.read(320)
    assert data != oldest, "the oldest block survived a full queue"
    assert overflowed, "audio was dropped and the reader was not told"

    remaining = [stream.read(320)[0] for _ in range(_SoundDeviceStream.MAX_BLOCKS - 1)]
    assert remaining[-1] == newest, "the newest block was the one thrown away"


def test_portaudio_reporting_a_status_is_recorded_as_lost_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's own overflow flag, which is separate from this queue filling up."""
    stream, on_audio = opened_stream(monkeypatch)

    on_audio(LOUD_FRAME, 320, None, "input overflow")

    assert stream.read(320)[1]


def test_a_falsy_status_is_not_an_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """PortAudio passes a status object on every callback; only a truthy one means trouble,
    and treating the ordinary case as a fault would mark every frame as lost audio."""
    stream, on_audio = opened_stream(monkeypatch)

    on_audio(LOUD_FRAME, 320, None, "")

    assert not stream.read(320)[1]
