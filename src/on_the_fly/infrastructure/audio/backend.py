"""The seam between this project and whatever actually talks to a sound card.

`CaptureBackend` is a deliberately tiny interface — open a stream, read from it, close it.
Small enough that a fake backend in a test is a dozen lines, which is what makes the
microphone adapter testable on a machine with no microphone, and in CI, which has none.

`sounddevice` is imported lazily inside `SoundDeviceBackend`, never at module scope. The
domain layer and every test that does not specifically exercise this backend therefore run
with no audio library present at all (ADR 0002, ADR 0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# 16-bit signed PCM, matching AudioFormat. The string is PortAudio's own spelling.
PCM_DTYPE = "int16"


class AudioDeviceError(Exception):
    """The audio device could not be opened, read, or is no longer available.

    One error type for every backend failure, so callers handle "the microphone stopped
    working" once rather than catching a different library's exception hierarchy. The
    original exception is chained, so nothing is lost for diagnosis.
    """


@runtime_checkable
class InputStream(Protocol):
    """An open capture stream."""

    def start(self) -> None:
        """Begin capturing. Idempotent."""
        ...

    def read(self, frames: int) -> tuple[bytes, bool]:
        """Read exactly `frames` frames.

        Returns the PCM bytes and whether the device reported an overflow — meaning input
        was discarded because it was not read fast enough. The flag is passed up rather
        than swallowed: dropped audio is a real event that should be counted, not hidden.
        """
        ...

    def close(self) -> None:
        """Stop and release the stream. Idempotent."""
        ...


class CaptureBackend(Protocol):
    """Something that can open capture streams."""

    @property
    def name(self) -> str:
        """A short identifier for diagnostics, e.g. "sounddevice". Not a device name."""
        ...

    def open_input_stream(
        self,
        *,
        sample_rate_hz: int,
        channels: int,
        blocksize: int,
        device: int | str | None = None,
    ) -> InputStream:
        """Open a capture stream, or raise `AudioDeviceError`."""
        ...


class _SoundDeviceStream:
    """Wraps a `sounddevice.RawInputStream` behind `InputStream`."""

    __slots__ = ("_closed", "_started", "_stream")

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise AudioDeviceError("cannot start a stream that has been closed")
        if self._started:
            return
        try:
            self._stream.start()
        except Exception as exc:
            raise AudioDeviceError(f"could not start the audio stream: {exc}") from exc
        self._started = True

    def read(self, frames: int) -> tuple[bytes, bool]:
        if self._closed:
            raise AudioDeviceError("cannot read from a stream that has been closed")
        try:
            data, overflowed = self._stream.read(frames)
        except Exception as exc:
            # A device unplugged mid-sentence lands here. It is a normal thing for hardware
            # to do and becomes a clean, catchable outcome rather than a library-specific
            # exception leaking into the pipeline.
            raise AudioDeviceError(f"audio device read failed: {exc}") from exc
        return bytes(data), bool(overflowed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing must not raise. A failure here would mask whatever error caused the
        # shutdown, and the caller can do nothing useful about it either way.
        try:
            self._stream.stop()
        except Exception:  # noqa: S110 - see above
            pass
        try:
            self._stream.close()
        except Exception:  # noqa: S110 - see above
            pass


class SoundDeviceBackend:
    """The real backend: PortAudio via `sounddevice` (ADR 0003)."""

    __slots__ = ()

    @property
    def name(self) -> str:
        return "sounddevice"

    @staticmethod
    def _import_sounddevice() -> Any:
        """Import lazily, and turn a missing library into an actionable message.

        On Linux the wheel bundles no PortAudio binary, so a missing system package is the
        single most likely reason this fails. Saying so is more useful than an ImportError.
        """
        try:
            import sounddevice
        except OSError as exc:
            raise AudioDeviceError(
                "the PortAudio library could not be loaded. On Linux install the system "
                f"package libportaudio2. Underlying error: {exc}"
            ) from exc
        except ImportError as exc:
            raise AudioDeviceError(
                "the 'sounddevice' package is not installed; install the runtime "
                f"requirements. Underlying error: {exc}"
            ) from exc
        return sounddevice

    def open_input_stream(
        self,
        *,
        sample_rate_hz: int,
        channels: int,
        blocksize: int,
        device: int | str | None = None,
    ) -> InputStream:
        sounddevice = self._import_sounddevice()
        try:
            raw = sounddevice.RawInputStream(
                samplerate=sample_rate_hz,
                blocksize=blocksize,
                device=device,
                channels=channels,
                dtype=PCM_DTYPE,
            )
        except Exception as exc:
            raise AudioDeviceError(
                f"could not open an input stream at {sample_rate_hz}Hz: {exc}"
            ) from exc
        return _SoundDeviceStream(raw)

    def input_device_names(self) -> Sequence[str]:
        """Names of available input devices, for a device picker.

        These are shown to the user who owns the hardware. They are **not** safe for logs,
        metrics or telemetry: people name devices after themselves, so a device name can
        identify a person (ADR 0003).
        """
        sounddevice = self._import_sounddevice()
        try:
            devices = sounddevice.query_devices()
        except Exception as exc:
            raise AudioDeviceError(f"could not enumerate audio devices: {exc}") from exc

        names: list[str] = []
        for device in devices:
            if int(device.get("max_input_channels", 0)) > 0:
                names.append(str(device.get("name", "unknown")))
        return tuple(names)
