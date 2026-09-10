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
a byte of audio is used — and, since `truncated_seconds`, its claim about its own length is
checked against what the file actually handed over rather than taken on trust.
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


# The first bytes of the containers somebody is most likely to have a recording in. A user
# who points this at `interview.m4a` gets "does not start with RIFF id", which is true and
# tells them nothing they can act on — so the file is sniffed and the message says what it
# looks like and the one command that converts it (ADR 0046).
_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"ID3", 0, "an MP3 with an ID3 tag"),
    (b"\xff\xfb", 0, "an MP3"),
    (b"\xff\xf3", 0, "an MP3"),
    (b"OggS", 0, "an Ogg container — Opus or Vorbis"),
    (b"fLaC", 0, "a FLAC file"),
    (b"ftyp", 4, "an MP4 container — M4A, AAC or a video"),
    (b"FORM", 0, "an AIFF file"),
    (b"\x1aE\xdf\xa3", 0, "a Matroska container — MKV or WebM"),
)

# The conversion, spelled out. `-ac 1` because the pipeline is mono and mixing channels is a
# decision about which voice to keep; `-ar 16000` because that is what every pinned model
# takes, and doing it here saves a second conversion later.
_FFMPEG_HINT = "ffmpeg -i {name} -ac 1 -ar 16000 {stem}.wav"


def _what_it_looks_like(path: Path) -> str:
    """A sentence naming the format and how to convert it, or nothing when it is a mystery.

    Reads 12 bytes. A file that cannot be read at all says nothing extra rather than raising
    a second error on top of the first.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
    except OSError:  # pragma: no cover - the file was readable a moment ago
        return ""

    for signature, offset, description in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            command = _FFMPEG_HINT.format(name=path.name, stem=path.stem)
            return f" It looks like {description}. Convert it first:\n  {command}"

    if head[:4] == b"RIFF":
        return " The file is a RIFF container but not WAVE audio."
    return " This reader takes WAV only; convert other formats with ffmpeg first."


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
        # Set only when `frames()` runs off the end of the file. Until then, audio not yet
        # read is simply not yet read, and nothing can be concluded about what is missing.
        self._reached_end = False

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
                # `wave` counts single samples here; this module calls a 20ms block a frame.
                # Kept under the name that says which, because the two differ by 320x and
                # confusing them is how a truncation check ends up measuring nothing.
                self._declared_samples = declared_frames
        except wave.Error as exc:
            raise WavSourceError(
                f"not a readable WAV file: {exc}.{_what_it_looks_like(self._path)}"
            ) from exc
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

    @property
    def _samples_per_frame(self) -> int:
        return self._frame_bytes // self._format.sample_width_bytes

    @property
    def declared_seconds(self) -> float:
        """How much audio the header says this file carries."""
        return self._declared_samples / self._format.sample_rate_hz

    @property
    def delivered_seconds(self) -> float:
        """How much audio the file actually handed over."""
        return self._frames_yielded * self._frame_bytes / self._format.bytes_per_second

    @property
    def truncated_seconds(self) -> float:
        """Audio the header promised that the file did not carry.

        A WAV header states its own payload length, and until this existed nothing compared
        that statement against what arrived. A recording cut short — a recorder that
        crashed, a copy that stopped, a download that ended early — reads as a shorter
        recording, and every duration this project prints is computed from what arrived, so
        every one of them agrees with itself and none of them notices. Half a meeting can
        go missing and the transcript looks complete.

        The microphone path has always said `dropped N overflow(s) - audio was lost`. This
        is the same sentence for a file, and it was the one input the project called
        untrusted (Article 4) while leaving its size claim unchecked.

        Zero until `frames()` has reached the end of the file. Zero, too, for a shortfall
        under one frame: `frames()` discards a trailing partial frame on purpose, so up to
        one frame missing is that decision rather than a damaged file.
        """
        if not self._reached_end:
            return 0.0
        missing = self._declared_samples - self._frames_yielded * self._samples_per_frame
        if missing < self._samples_per_frame:
            return 0.0
        return missing / self._format.sample_rate_hz

    @property
    def is_truncated(self) -> bool:
        """True when the file carried materially less audio than its header declared."""
        return self.truncated_seconds > 0.0

    def __repr__(self) -> str:
        # The file name is shown: the user chose this path, and without it a diagnostic
        # about which file failed is useless. The audio itself never appears.
        return (
            f"WavFileSource(path={self._path.name!r}, "
            f"rate={self._format.sample_rate_hz}, frame_ms={self._frame_ms})"
        )

    def frames(self) -> Generator[bytes]:
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
                    # End of file, or a partial trailing frame. Either way, stop — but note
                    # that the end was reached, which is what makes the shortfall below
                    # mean anything.
                    self._reached_end = True
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
