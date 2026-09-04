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

    def default_sample_rate(self, device: int | str | None = None) -> int | None:
        """The device's own preferred rate, or None if it cannot be determined.

        Used to negotiate rather than demand (ADR 0013): a device that offers 48 kHz should
        be asked for 48 kHz rather than refused. Returning None is not an error — it means
        the caller falls back to a candidate list.
        """
        ...

    def supports_rate(
        self, sample_rate_hz: int, *, channels: int, device: int | str | None = None
    ) -> bool:
        """Whether the device accepts this rate, **without opening a stream**.

        Negotiation probes rather than opening and retrying, because repeatedly opening a
        stream that fails corrupts the heap in PortAudio's ALSA backend — observed as
        `malloc(): mismatching next->prev_size` and a core dump after four failed opens in
        one process (ADR 0013). Probing is cheap and does not touch the allocator path that
        breaks.
        """
        ...


class _SoundDeviceStream:
    """Wraps a callback-driven `sounddevice.RawInputStream` behind `InputStream`.

    **Callback, not blocking reads.** PortAudio's blocking `read()` aborts the process on
    some devices — `malloc(): unsorted double linked list corrupted`, a `SIGABRT` no
    `except` can catch. It reproduced with raw `sounddevice` and no project code, on a
    4-channel device opened as mono, while `sounddevice.rec()` on the same device at the
    same rate worked; `rec()` uses the callback API, and so does this (ADR 0015).

    The callback runs on PortAudio's own high-priority thread. It does the least possible:
    copy the bytes, put them in a queue, return. Anything slower there causes drop-outs in
    the audio itself, so no parsing, no allocation beyond the copy, and no logging.

    The queue is **bounded**. When a consumer falls behind, blocks are dropped and counted
    rather than accumulating — unbounded buffering would trade a drop-out for a memory leak
    and hand the recogniser audio that is seconds stale, which for a live translator is
    worse than losing it.
    """

    __slots__ = ("_closed", "_dropped", "_queue", "_started", "_status_overflows", "_stream")

    # Two seconds at 20 ms blocks. Enough to ride out a scheduling hiccup, short enough
    # that a consumer which stalls loses audio rather than silently building latency.
    MAX_BLOCKS = 100

    def __init__(self, stream: Any, queue_: Any) -> None:
        self._stream = stream
        self._queue = queue_
        self._started = False
        self._closed = False
        self._dropped = 0
        self._status_overflows = 0

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
        """Take the next block the callback delivered.

        `frames` is not used to size the read: the callback delivers whatever block size
        the stream was opened with, which is the size the caller asked for. It stays in the
        signature because the port is shared with backends that do size their reads.
        """
        if self._closed:
            raise AudioDeviceError("cannot read from a stream that has been closed")

        import queue as _queue

        try:
            data = self._queue.get(timeout=5.0)
        except _queue.Empty:
            # Five seconds of silence from a device that claimed to be running is a failure,
            # not a quiet room: the callback delivers blocks whether or not anyone is
            # speaking. A device unplugged mid-sentence lands here.
            raise AudioDeviceError(
                "the audio device stopped delivering audio; it may have been disconnected"
            ) from None

        dropped = self._dropped
        self._dropped = 0
        overflowed = dropped > 0 or self._status_overflows > 0
        self._status_overflows = 0
        return data, overflowed

    def note_drop(self) -> None:
        """Called from the audio callback when the queue is full. Must stay trivial."""
        self._dropped += 1

    def note_status_overflow(self) -> None:
        """Called from the audio callback when PortAudio itself reports lost input."""
        self._status_overflows += 1

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
        # Release anything still queued. Captured audio is EPHEMERAL and there is no reason
        # for it to outlive the device that produced it.
        try:
            while True:
                self._queue.get_nowait()
        except Exception:  # noqa: S110 - queue empty
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
        import queue as _queue

        sounddevice = self._import_sounddevice()
        blocks: _queue.Queue[bytes] = _queue.Queue(maxsize=_SoundDeviceStream.MAX_BLOCKS)
        wrapper: dict[str, _SoundDeviceStream] = {}

        def on_audio(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            """Runs on PortAudio's audio thread. Keep it to a copy and a put.

            Anything slower here — allocation, logging, a lock held by another thread —
            shows up as a drop-out in the captured audio itself.
            """
            stream = wrapper.get("stream")
            if status and stream is not None:
                stream.note_status_overflow()
            try:
                blocks.put_nowait(bytes(indata))
            except _queue.Full:
                # Consumer is behind. Drop the oldest audio rather than the newest: for a
                # live translator the most recent speech is the useful part.
                try:
                    blocks.get_nowait()
                    blocks.put_nowait(bytes(indata))
                except Exception:  # noqa: S110 - raced with the consumer; the block is lost
                    pass
                if stream is not None:
                    stream.note_drop()

        try:
            raw = sounddevice.RawInputStream(
                samplerate=sample_rate_hz,
                blocksize=blocksize,
                device=device,
                channels=channels,
                dtype=PCM_DTYPE,
                callback=on_audio,
            )
        except Exception as exc:
            raise AudioDeviceError(
                f"could not open an input stream at {sample_rate_hz}Hz: {exc}"
            ) from exc

        stream = _SoundDeviceStream(raw, blocks)
        wrapper["stream"] = stream
        return stream

    def default_sample_rate(self, device: int | str | None = None) -> int | None:
        sounddevice = self._import_sounddevice()
        try:
            info = sounddevice.query_devices(
                device if device is not None else sounddevice.default.device[0]
            )
            rate = int(float(info["default_samplerate"]))
        except Exception:
            # Not knowing the native rate is a reason to try the candidate list, not a
            # reason to fail before the device has been asked for anything.
            return None
        return rate if rate > 0 else None

    def supports_rate(
        self, sample_rate_hz: int, *, channels: int, device: int | str | None = None
    ) -> bool:
        sounddevice = self._import_sounddevice()
        try:
            sounddevice.check_input_settings(
                device=device,
                samplerate=sample_rate_hz,
                channels=channels,
                dtype=PCM_DTYPE,
            )
        except Exception:
            return False
        return True

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
