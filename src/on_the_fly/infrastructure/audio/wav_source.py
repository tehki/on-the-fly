"""A WAV file as an `AudioSource`.

The pipeline has had no way to hear anything reproducible. A microphone is live, unrepeatable,
and absent entirely on machines without an input device; a file is none of those things. This
adapter is what makes the pipeline runnable end to end, testable against known audio, and
measurable against `docs/PERFORMANCE_BUDGET.md`.

Stdlib `wave` only — no new dependency for something the standard library already does.

Two deliberate refusals:

**No resampling.** A file at 44.1 kHz is exposed as 44.1 kHz, not silently converted. Sample
rate conversion changes the audio a recogniser will see, and doing it invisibly inside a file
reader is how a model ends up being fed something nobody chose. The caller decides.

**No whole-file read.** Frames are streamed. A long recording must not become a memory
decision, and the pipeline above is built for a stream anyway.

A WAV file is untrusted input like any other (Article 4). Its header is attacker-controlled
if the file came from anywhere but the user's own recorder, so the header is validated before
a byte of audio is used.
"""

from __future__ import annotations

import wave
from collections.abc import Generator
from pathlib import Path
from types import TracebackType

from on_the_fly.domain.audio import AudioFormat

DEFAULT_FRAME_MS = 20

# A guard against a pathological header rather than a real limit on recordings: a WAV
# claiming a multi-gigabyte frame count still only costs us one refusal here.
MAX_DECLARED_FRAMES = 500_000_000


class WavSourceError(Exception):
    """The file could not be opened, or is not audio this pipeline can use."""


class WavFileSource:
    """Reads PCM frames from a WAV file."""

    def __init__(
        self,
        path: Path | str,
        *,
        frame_ms: int = DEFAULT_FRAME_MS,
        allowed_root: Path | str | None = None,
    ) -> None:
        self._path = self._resolve(path, allowed_root)
        self._frame_ms = frame_ms
        self._format = self._read_format()
        # Raises before any audio is read if the duration is not a whole number of samples.
        self._frame_bytes = self._format.frame_bytes(frame_ms)
        self._reader: wave.Wave_read | None = None
        self._closed = False
        self._frames_yielded = 0

    @staticmethod
    def _resolve(path: Path | str, allowed_root: Path | str | None) -> Path:
        """Resolve the path and, when a root is given, confine it to that root.

        `allowed_root` is optional because a person naming a file on their own command line
        is choosing it deliberately. It exists for every other caller — a queue, a watched
        directory, an API — where the path arrives from somewhere less trustworthy and
        traversal is a real concern (handbook 56).
        """
        resolved = Path(path).expanduser().resolve()

        if allowed_root is not None:
            root = Path(allowed_root).expanduser().resolve()
            if not resolved.is_relative_to(root):
                # Compared after resolution, so `../` and symlinks are already collapsed.
                raise WavSourceError(f"refusing to read outside the allowed root: {root}")

        if not resolved.exists():
            raise WavSourceError(f"no such file: {resolved}")
        if not resolved.is_file():
            raise WavSourceError(f"not a regular file: {resolved}")
        return resolved

    def _read_format(self) -> AudioFormat:
        """Validate the header and turn it into an `AudioFormat`."""
        try:
            with wave.open(str(self._path), "rb") as reader:
                channels = reader.getnchannels()
                sample_width = reader.getsampwidth()
                frame_rate = reader.getframerate()
                declared_frames = reader.getnframes()
        except wave.Error as exc:
            raise WavSourceError(f"not a readable WAV file: {exc}") from exc
        except OSError as exc:
            raise WavSourceError(f"could not open the file: {exc}") from exc

        if declared_frames > MAX_DECLARED_FRAMES:
            raise WavSourceError(
                f"WAV header declares {declared_frames} frames, above the "
                f"{MAX_DECLARED_FRAMES} sanity limit"
            )

        try:
            # AudioFormat enforces mono and 16-bit; a stereo or 24-bit file is rejected
            # here with the format's own message rather than being downmixed by accident.
            return AudioFormat(
                sample_rate_hz=frame_rate,
                channels=channels,
                sample_width_bytes=sample_width,
            )
        except ValueError as exc:
            raise WavSourceError(f"{self._path.name}: {exc}") from exc

    @property
    def path(self) -> Path:
        return self._path

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    @property
    def frames_yielded(self) -> int:
        return self._frames_yielded

    def __repr__(self) -> str:
        # The file name is shown: the user chose this path, and without it a diagnostic
        # about which file failed is useless. The audio itself never appears.
        return (
            f"WavFileSource(path={self._path.name!r}, "
            f"rate={self._format.sample_rate_hz}, frame_ms={self._frame_ms})"
        )

    def frames(self) -> Generator[bytes, None, None]:
        """Yield fixed-size frames until the file ends.

        A trailing partial frame is discarded rather than padded. Padding invents audio,
        and a fraction of a frame at the end of a recording carries nothing worth keeping.
        """
        if self._closed:
            raise WavSourceError("this source has been closed; construct a new one to read again")
        if self._reader is not None:
            raise WavSourceError("this source is already being read")

        samples_per_frame = self._frame_bytes // self._format.sample_width_bytes
        try:
            reader = wave.open(str(self._path), "rb")
        except (wave.Error, OSError) as exc:
            raise WavSourceError(f"could not open the file: {exc}") from exc

        self._reader = reader
        try:
            while not self._closed:
                chunk = reader.readframes(samples_per_frame)
                if len(chunk) < self._frame_bytes:
                    # End of file, or a partial trailing frame. Either way, stop.
                    break
                self._frames_yielded += 1
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        """Release the file handle. Safe to call more than once."""
        self._closed = True
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.close()

    def __enter__(self) -> WavFileSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
