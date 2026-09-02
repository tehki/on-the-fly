"""Speech recognition via faster-whisper, behind the domain's `SpeechRecognizer` port.

The only place in this project that knows Whisper exists. `domain/audio/ports.py` declares
what a recogniser is; nothing above this file learns which one it got (ADR 0002).

Two decisions worth knowing before changing them.

**The bundled VAD is not used.** faster-whisper can run Silero VAD inside `transcribe()`
and re-cut the audio it is given. This project already decided where utterances begin and
end, in `UtteranceSegmenter`, under bounds it can explain. Letting the recogniser silently
re-segment would put that decision in two places, and the second one would win without
appearing in any of our tests.

**No resampling.** Whisper expects 16 kHz. Audio at another rate is refused rather than
converted here, the same refusal `WavFileSource` makes and for the same reason: the
conversion changes what the model sees, so it is a decision for whoever chose the input,
not a side effect of the recogniser.

Transcripts are `EPHEMERAL` project content the moment they exist. This module returns
text to its caller and writes it nowhere — no log, no cache, no exception message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from on_the_fly.domain.audio import AudioFormat

# What the model was trained on. Not a preference.
REQUIRED_SAMPLE_RATE_HZ = 16_000

# int16 full scale. Whisper wants float32 in [-1, 1).
_INT16_FULL_SCALE = 32768.0


class RecognitionError(Exception):
    """The recogniser could not load, or could not process the audio it was given."""


class FasterWhisperRecognizer:
    """Transcribes utterance audio with a locally stored, verified Whisper model."""

    def __init__(
        self,
        model_dir: Path | str,
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        language: str | None = None,
    ) -> None:
        if beam_size < 1:
            raise ValueError("beam_size must be at least 1")

        self._model_dir = Path(model_dir)
        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._language = language
        self._model: Any | None = None

    @property
    def model_dir(self) -> Path:
        return self._model_dir

    def __repr__(self) -> str:
        return (
            f"FasterWhisperRecognizer(model={self._model_dir.name!r}, "
            f"device={self._device!r}, compute_type={self._compute_type!r}, "
            f"loaded={self._model is not None})"
        )

    def _load(self) -> Any:
        """Load the model on first use.

        Lazy because loading costs hundreds of milliseconds and a few hundred megabytes,
        and a session that never hears speech should pay neither. The import is lazy for
        the same reason the audio backend's is: the domain and its tests run without it.
        """
        if self._model is not None:
            return self._model

        if not self._model_dir.is_dir():
            raise RecognitionError(f"model directory does not exist: {self._model_dir}")

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RecognitionError(
                "faster-whisper is not installed; install the runtime requirements. "
                f"Underlying error: {exc}"
            ) from exc

        try:
            self._model = WhisperModel(
                str(self._model_dir),
                device=self._device,
                compute_type=self._compute_type,
                # Never reach the network from here. The model is placed on disk by
                # ModelStore, which verified it; a loader that could also download would
                # be a second, unverified path to the same thing.
                local_files_only=True,
            )
        except Exception as exc:
            raise RecognitionError(f"could not load the model: {exc}") from exc
        return self._model

    def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
        """Return the text of one utterance. Empty when nothing was recognised."""
        if audio_format.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise RecognitionError(
                f"this model expects {REQUIRED_SAMPLE_RATE_HZ} Hz audio but was given "
                f"{audio_format.sample_rate_hz} Hz. Resample deliberately upstream; this "
                "recogniser will not do it silently."
            )
        if not audio:
            return ""

        model = self._load()

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover - numpy arrives with faster-whisper
            raise RecognitionError(f"numpy is required to pass audio to the model: {exc}") from exc

        samples = numpy.frombuffer(audio, dtype=numpy.int16).astype(numpy.float32)
        samples /= _INT16_FULL_SCALE

        try:
            segments, _info = model.transcribe(
                samples,
                beam_size=self._beam_size,
                language=self._language,
                # See the module docstring: segmentation already happened, upstream, under
                # bounds this project can explain.
                vad_filter=False,
            )
            # `segments` is a generator; the work happens as it is consumed.
            text = " ".join(segment.text.strip() for segment in segments)
        except Exception as exc:
            # The message deliberately says nothing about the audio or any partial result.
            raise RecognitionError(f"transcription failed: {type(exc).__name__}") from exc

        return text.strip()
