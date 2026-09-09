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

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from on_the_fly.domain.audio import INT16_NORMALISATION_SCALE, AudioFormat

# What the model was trained on. Not a preference.
REQUIRED_SAMPLE_RATE_HZ = 16_000


class RecognitionError(Exception):
    """The recogniser could not load, or could not process the audio it was given."""


# What faster-whisper does when its own quality checks reject a decode: it retries the segment
# at a higher temperature, which means sampling instead of taking the best token. A segment
# that comes back above zero is one the model's greedy decode failed and it fell back on.
#
# That, and not the log probability, is the signal. Measured on the three published French
# clips and the two English ones, three runs each: every clip that comes back correct is
# decoded at temperature 0.0 every time, and the one that comes back as a *different sentence*
# is decoded at 0.2 or 1.0 every time. The log probability after the fallback is much weaker —
# it crossed the publisher's -1.0 threshold in only one run of five on the same clip, because
# the number reported is the one the retry settled for (ADR 0042).
GREEDY_TEMPERATURE = 0.0

# OpenAI's threshold for a failed decode, kept because the number it applies to is worth
# reporting even when the fallback flag is the thing being acted on.
FAILED_DECODE_CONFIDENCE = -1.0


@dataclass(frozen=True)
class Transcription:
    """One utterance, and what the model thought of its own answer.

    `confidence` is a mean log probability: 0 is certain, and more negative is less sure.
    `temperature` is the highest any segment needed — above zero means the model's own quality
    checks rejected the greedy decode and it sampled instead. Both are `None` when there was
    nothing to decode, which is not the same as being sure about silence.
    """

    text: str
    confidence: float | None
    no_speech: float | None
    temperature: float | None = None

    @property
    def is_failed_decode(self) -> bool:
        """Whether the model fell back to sampling, having rejected its own first answer."""
        return self.temperature is not None and self.temperature > GREEDY_TEMPERATURE


def _weighted_confidence(segments: Sequence[Any]) -> float | None:
    """Mean `avg_logprob` weighted by segment duration, or `None` if there is nothing.

    Weighted because a segment covering nine seconds describes more of the utterance than one
    covering half a second, and an unweighted mean lets a short confident fragment speak for a
    long uncertain one.
    """
    if not segments:
        return None
    weights: list[float] = [
        max(float(segment.end) - float(segment.start), 0.0) for segment in segments
    ]
    total = sum(weights)
    if total <= 0:
        return sum(float(segment.avg_logprob) for segment in segments) / len(segments)
    weighted = [
        float(segment.avg_logprob) * weight
        for segment, weight in zip(segments, weights, strict=True)
    ]
    return sum(weighted) / total


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
        return self.transcribe_with_confidence(audio, audio_format).text

    def transcribe_with_confidence(self, audio: bytes, audio_format: AudioFormat) -> Transcription:
        """The text, and what the model thought of it (ADR 0042).

        Whisper reports `avg_logprob` per segment and OpenAI's own implementation treats a
        value below -1.0 as a failed decode. This project had no way at all to notice a
        recogniser inventing a sentence — ADR 0021 said so plainly — and this is one, for the
        batch tier, for free, out of a field that was being discarded.

        The utterance's figure is the duration-weighted mean of its segments, because a long
        segment's confidence describes more of the utterance than a short one's.
        """
        if audio_format.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise RecognitionError(
                f"this model expects {REQUIRED_SAMPLE_RATE_HZ} Hz audio but was given "
                f"{audio_format.sample_rate_hz} Hz. Resample deliberately upstream; this "
                "recogniser will not do it silently."
            )
        if not audio:
            return Transcription(text="", confidence=None, no_speech=None, temperature=None)

        model = self._load()

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover - numpy arrives with faster-whisper
            raise RecognitionError(f"numpy is required to pass audio to the model: {exc}") from exc

        samples = numpy.frombuffer(audio, dtype=numpy.int16).astype(numpy.float32)
        samples /= INT16_NORMALISATION_SCALE

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
            decoded = list(segments)
        except Exception as exc:
            # The message deliberately says nothing about the audio or any partial result.
            raise RecognitionError(f"transcription failed: {type(exc).__name__}") from exc

        text = " ".join(segment.text.strip() for segment in decoded).strip()
        return Transcription(
            text=text,
            confidence=_weighted_confidence(decoded),
            no_speech=max((float(segment.no_speech_prob) for segment in decoded), default=None),
            # The worst of them: one segment the model gave up on is an utterance it gave up
            # on, however confident it was about the rest.
            temperature=max((float(segment.temperature) for segment in decoded), default=None),
        )
