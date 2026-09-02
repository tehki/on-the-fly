"""A microphone as an `AudioSource`.

The adapter that finally lets the pipeline hear something. It implements the domain's
`AudioSource` port and holds all the device-specific awkwardness — overflow flags, lazy
opening, deterministic closing — so the pipeline above it stays unchanged whether it is
reading from a laptop microphone, a file, or a test fixture.

Two behaviours are deliberate and worth knowing before changing them.

**The device is opened when capture starts, not when the object is built.** Constructing a
`MicrophoneSource` touches no hardware. An application can build one at startup, put it in
a settings screen, and never open the microphone until someone presses record. An
application that holds a microphone open while idle is a privacy problem whether or not it
reads from it, and the indicator light on the user's machine is the visible part of that.

**Overflows are counted, not hidden.** When the consumer cannot keep up, PortAudio discards
input and says so. That is lost audio — a dropped word — and it is surfaced as a counter
rather than swallowed. If the number climbs, the pipeline is too slow, and that is
information the performance budget needs.
"""

from __future__ import annotations

from collections.abc import Generator
from types import TracebackType

from on_the_fly.domain.audio import AudioFormat
from on_the_fly.infrastructure.audio.backend import (
    AudioDeviceError,
    CaptureBackend,
    InputStream,
    SoundDeviceBackend,
)

DEFAULT_FRAME_MS = 20


class MicrophoneSource:
    """Captures PCM frames from an input device."""

    def __init__(
        self,
        *,
        audio_format: AudioFormat | None = None,
        frame_ms: int = DEFAULT_FRAME_MS,
        backend: CaptureBackend | None = None,
        device: int | str | None = None,
    ) -> None:
        self._format = audio_format if audio_format is not None else AudioFormat()
        # Raises if the duration is not a whole number of samples, before any device is
        # touched. A frame size that disagrees with the sample rate produces audio that
        # still plays and translates badly, with nothing in the logs to explain it.
        self._frame_bytes = self._format.frame_bytes(frame_ms)
        self._frame_ms = frame_ms
        self._backend: CaptureBackend = backend if backend is not None else SoundDeviceBackend()
        self._device = device

        self._stream: InputStream | None = None
        self._closed = False
        self._overflow_count = 0
        self._frames_yielded = 0

    # -- description -------------------------------------------------------------------

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def overflow_count(self) -> int:
        """How many reads reported discarded input. `OPERATIONAL_METADATA`: a count."""
        return self._overflow_count

    @property
    def frames_yielded(self) -> int:
        return self._frames_yielded

    @property
    def is_open(self) -> bool:
        return self._stream is not None and not self._closed

    def __repr__(self) -> str:
        # No device name: a device name can identify a person (ADR 0003), and a repr ends
        # up in tracebacks and bug reports.
        return (
            f"MicrophoneSource(backend={self._backend.name!r}, "
            f"rate={self._format.sample_rate_hz}, frame_ms={self._frame_ms}, "
            f"open={self.is_open}, overflows={self._overflow_count})"
        )

    # -- capture -----------------------------------------------------------------------

    def frames(self) -> Generator[bytes, None, None]:
        """Open the device and yield frames until closed.

        Raises `AudioDeviceError` if the device cannot be opened or fails mid-stream. The
        device is released on every exit path, including that one.
        """
        if self._closed:
            raise AudioDeviceError(
                "this microphone source has been closed; construct a new one to capture again"
            )
        if self._stream is not None:
            raise AudioDeviceError("this microphone source is already capturing")

        samples_per_frame = self._frame_bytes // self._format.sample_width_bytes
        stream = self._backend.open_input_stream(
            sample_rate_hz=self._format.sample_rate_hz,
            channels=self._format.channels,
            blocksize=samples_per_frame,
            device=self._device,
        )
        self._stream = stream

        try:
            stream.start()
            while not self._closed:
                try:
                    data, overflowed = stream.read(samples_per_frame)
                except AudioDeviceError:
                    if self._closed:
                        # `close()` was called from another thread while a read was in
                        # flight. That is a stop, not a failure: ending quietly is the
                        # correct outcome, and raising here would turn every clean
                        # shutdown into an error.
                        break
                    raise
                if overflowed:
                    # Input was discarded because we were too slow. Counted, never hidden.
                    self._overflow_count += 1
                if not data:
                    # A backend that returns nothing has stopped producing; treat it as
                    # end of stream rather than spinning on an empty read.
                    break
                self._frames_yielded += 1
                yield data
        finally:
            # Runs on normal end, on error, and when the caller stops consuming. The
            # microphone is not left open in any of those cases.
            self.close()

    def close(self) -> None:
        """Release the device. Safe to call more than once, and from anywhere."""
        self._closed = True
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

    def __enter__(self) -> MicrophoneSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
