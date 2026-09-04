"""Genuine streaming recognition, via sherpa-onnx.

The first recogniser in this project that can emit text while someone is still speaking.
Where `BatchStreamingRecognizer` waits for an utterance and then runs a whole inference,
this consumes 20 ms frames and produces a growing hypothesis as it goes.

It implements `StreamingRecognizer` from the domain, so nothing above it learns that
sherpa-onnx exists (ADR 0002, ADR 0006).

Two differences from the Whisper path that matter to callers:

**It emits partials.** `emits_partials` is True, and the text of a partial can change — the
model revises its own hypothesis as more audio arrives. A caption renderer must handle text
being replaced, not appended.

**It does its own endpointing.** A transducer decides where an utterance ends from the audio
itself, so `UtteranceSegmenter` is not in this path. That is deliberate: two endpointers
disagreeing would produce cuts nobody could explain. The segmenter still owns the batch
path, and the retention bound it provides there is replaced here by the recogniser's own
bounded internal buffers.

Transcripts are `EPHEMERAL` content the moment they exist. This module returns them and
writes them nowhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from on_the_fly.domain.audio import AudioFormat, TranscriptEvent

# What the pinned Zipformer models were trained on.
REQUIRED_SAMPLE_RATE_HZ = 16_000

# int16 full scale; sherpa-onnx wants float32 in [-1, 1).
_INT16_FULL_SCALE = 32768.0


@dataclass(frozen=True)
class StreamingLayout:
    """Which file in a pinned model directory plays which role.

    Named per model rather than discovered by globbing: a recogniser that loads whatever
    ONNX file it finds would happily load something nobody pinned. It became a parameter
    when the second language arrived — the English model names its files after a training
    epoch, the Russian one after a chunk size, and neither is a convention.
    """

    encoder: str
    decoder: str
    joiner: str
    tokens: str = "tokens.txt"


# The English pin (ADR 0008). Kept as the default so existing callers are unaffected.
ENCODER_FILE = "encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx"
DECODER_FILE = "decoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx"
JOINER_FILE = "joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx"
TOKENS_FILE = "tokens.txt"

ENGLISH_LAYOUT = StreamingLayout(
    encoder=ENCODER_FILE, decoder=DECODER_FILE, joiner=JOINER_FILE, tokens=TOKENS_FILE
)


class StreamingRecognitionError(Exception):
    """The streaming recogniser could not load, or could not process its audio."""


class SherpaStreamingRecognizer:
    """A streaming transducer that emits partial and final results."""

    def __init__(
        self,
        model_dir: Path | str,
        *,
        num_threads: int = 1,
        decoding_method: str = "greedy_search",
        layout: StreamingLayout = ENGLISH_LAYOUT,
    ) -> None:
        if num_threads < 1:
            raise ValueError("num_threads must be at least 1")

        self._model_dir = Path(model_dir)
        self._num_threads = num_threads
        self._decoding_method = decoding_method
        self._layout = layout

        self._recognizer: Any | None = None
        self._stream: Any | None = None
        self._utterances = 0
        self._last_partial = ""
        self._audio_seconds = 0.0
        self._utterance_started_at = 0.0

    @property
    def emits_partials(self) -> bool:
        """True. Callers must expect text to be replaced, not appended."""
        return True

    @property
    def utterances_seen(self) -> int:
        return self._utterances

    def __repr__(self) -> str:
        return (
            f"SherpaStreamingRecognizer(model={self._model_dir.name!r}, "
            f"threads={self._num_threads}, loaded={self._recognizer is not None})"
        )

    def _load(self) -> Any:
        """Load the model on first frame. Lazy, for the same reason Whisper's is."""
        if self._recognizer is not None:
            return self._recognizer

        try:
            import sherpa_onnx
        except ImportError as exc:
            raise StreamingRecognitionError(
                "sherpa-onnx is not installed; install the runtime requirements. "
                f"Underlying error: {exc}"
            ) from exc

        missing = [
            name
            for name in (
                self._layout.encoder,
                self._layout.decoder,
                self._layout.joiner,
                self._layout.tokens,
            )
            if not (self._model_dir / name).is_file()
        ]
        if missing:
            raise StreamingRecognitionError(
                f"model directory {self._model_dir} is missing: {', '.join(missing)}"
            )

        try:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(self._model_dir / self._layout.tokens),
                encoder=str(self._model_dir / self._layout.encoder),
                decoder=str(self._model_dir / self._layout.decoder),
                joiner=str(self._model_dir / self._layout.joiner),
                num_threads=self._num_threads,
                sample_rate=REQUIRED_SAMPLE_RATE_HZ,
                feature_dim=80,
                decoding_method=self._decoding_method,
                # The transducer's own endpointing. This is what makes it streaming rather
                # than a faster batch model.
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=1.2,
                rule3_min_utterance_length=300,
            )
        except Exception as exc:
            raise StreamingRecognitionError(f"could not load the streaming model: {exc}") from exc

        self._stream = self._recognizer.create_stream()
        return self._recognizer

    def warm_up(self) -> None:
        """Load the model now rather than on the first frame.

        Loading costs seconds and happens once per session. A caller that measures
        throughput wants that cost outside its timer, and a caller that wants a responsive
        first utterance wants it paid before audio arrives. Both are served by asking.
        """
        self._load()

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        """Feed one frame and return whatever became available."""
        if not frame:
            return ()

        recognizer = self._load()
        stream = self._stream
        if stream is None:  # pragma: no cover - _load always creates one
            raise StreamingRecognitionError("no active stream")

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover - numpy arrives with the stack
            raise StreamingRecognitionError(f"numpy is required: {exc}") from exc

        samples = numpy.frombuffer(frame, dtype=numpy.int16).astype(numpy.float32)
        samples /= _INT16_FULL_SCALE
        frame_seconds = len(samples) / REQUIRED_SAMPLE_RATE_HZ

        if self._audio_seconds == 0.0:
            self._utterance_started_at = 0.0
        self._audio_seconds += frame_seconds

        stream.accept_waveform(REQUIRED_SAMPLE_RATE_HZ, samples)

        events: list[TranscriptEvent] = []
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        text = recognizer.get_result(stream).strip()
        is_endpoint = recognizer.is_endpoint(stream)

        if is_endpoint:
            if text:
                self._utterances += 1
                events.append(self._event(text, is_final=True))
            # Reset the decoder state so the next utterance starts clean. Without this the
            # transducer keeps accumulating and every "final" repeats everything before it.
            recognizer.reset(stream)
            self._last_partial = ""
            self._utterance_started_at = self._audio_seconds
        elif text and text != self._last_partial:
            # Only when the hypothesis actually changed. Re-emitting an unchanged partial
            # makes a caption flicker for no reason.
            self._last_partial = text
            events.append(self._event(text, is_final=False))

        return tuple(events)

    def finish(self) -> Sequence[TranscriptEvent]:
        """End the stream and return any remaining hypothesis as a final."""
        if self._recognizer is None or self._stream is None:
            return ()

        recognizer = self._recognizer
        stream = self._stream
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        text = recognizer.get_result(stream).strip()
        if not text:
            return ()

        self._utterances += 1
        return (self._event(text, is_final=True),)

    def reset(self) -> None:
        """Discard in-flight state. The loaded model is kept; reloading costs seconds."""
        if self._recognizer is not None:
            self._stream = self._recognizer.create_stream()
        self._utterances = 0
        self._last_partial = ""
        self._audio_seconds = 0.0
        self._utterance_started_at = 0.0

    def _event(self, text: str, *, is_final: bool) -> TranscriptEvent:
        return TranscriptEvent(
            utterance_index=max(1, self._utterances),
            text=text,
            is_final=is_final,
            audio_offset_seconds=self._utterance_started_at,
            # Streaming latency is not "time to run an inference" — the work happened as
            # the audio arrived. What a caller cares about is how far behind the audio the
            # text is, which for a partial is essentially nothing.
            latency_seconds=0.0,
        )

    def validate_format(self, audio_format: AudioFormat) -> None:
        """Refuse audio the model was not trained on, rather than resampling it."""
        if audio_format.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise StreamingRecognitionError(
                f"this model expects {REQUIRED_SAMPLE_RATE_HZ} Hz audio but was given "
                f"{audio_format.sample_rate_hz} Hz. Resample deliberately upstream."
            )
