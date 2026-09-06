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

from on_the_fly.domain.audio import AudioFormat, EndReason, TranscriptEvent

# What the pinned Zipformer models were trained on.
REQUIRED_SAMPLE_RATE_HZ = 16_000

# int16 full scale; sherpa-onnx wants float32 in [-1, 1).
# --- endpointing -----------------------------------------------------------------------
#
# All three are **seconds**. That is worth stating, because getting it wrong is silent: this
# file previously passed 300 for the utterance ceiling, in the belief that it counted frames.
# It counts seconds, so the ceiling was five minutes and could never fire, leaving 1.2 s of
# trailing silence as the only way an utterance could ever end. Somebody reading aloud does
# not pause that long, and thirty seconds of live speech came back as two finals — one of
# them fourteen seconds long, translated in a single lump after the speaker had stopped
# (ADR 0022).

# Silence before anything has been decoded. The publisher's default, untouched.
SILENCE_BEFORE_ANY_SPEECH_SECONDS = 2.4

# Silence after something has been decoded: the rule that ends an ordinary sentence, and the
# one that governs how promptly a translation appears. The publisher's default of 1.2 s is
# far too long for conversation. Measured over 30 s of live conversational speech — 75 pauses,
# durations only, no audio kept (ADR 0024):
#
#     p50 0.12s   p75 0.22s   p90 0.56s   longest 1.34s
#
#     threshold   pauses it ends an utterance on    resulting cadence
#        1.2s        2 of 75  ( 2.7%)               one per 15.0s
#        0.6s        7 of 75  ( 9.3%)               one per  4.3s
#        0.5s        9 of 75  (12.0%)               one per  3.3s
#        0.3s       14 of 75  (18.7%)               one per  2.1s
#
# Those pauses are two populations: gaps inside speech, clustered at 0.12-0.22 s, and sentence
# boundaries from about 0.56 s. The threshold belongs in the gap between them, which is why
# 0.5 rather than a rounder number — above the within-speech cluster, below the boundaries it
# is meant to catch. At the publisher's 1.2 the rule fires twice in thirty seconds, which is
# why the utterance ceiling was doing all the work.
SILENCE_AFTER_SPEECH_SECONDS = 0.5

# The ceiling, for speech that never pauses. Eight seconds is the shortest value tested that
# split no word in either sample: the cut lands wherever the clock says, so a shorter ceiling
# cuts mid-word more often — 6 s turns BROTHEL into "BRO" and "THEL", 4 s turns PUNISHED into
# "PUNISH" and "ED". It bounds how long a translation can be withheld, which is the cost this
# exists to bound; the caption itself keeps streaming as partials throughout.
MAX_UTTERANCE_SECONDS = 8.0

# One 20 ms frame, near enough. The ceiling is checked per frame, so an utterance ended by it
# lands on or just past the boundary rather than exactly on it.
_CEILING_TOLERANCE_SECONDS = 0.02

# --- the flush tail ---------------------------------------------------------------------
#
# Silence appended before the stream is closed, so the encoder can see past the last word.
#
# A transducer needs future frames to emit a symbol. When audio simply stops, the final
# chunk has no future, and `input_finished()` does not supply one — so the last word came
# out truncated or not at all. Measured on both pinned exports, decoding each publisher's
# own test set with a growing tail:
#
#     model       file        0 ms                     recovered at
#     english     0.wav       ...OF THE BROTHEL        300 ms  -> BROTHELS
#     english     1.wav       ...A BLESSED SOUL IN HE  100 ms  -> HEAVEN
#     french      19738183    ...DE L'HISTOIRE RO      100 ms  -> ROMAINE
#
# Over the whole of the English model's own test set that is **3.0% word error against
# 0.0%** — two files, two lost words, on the flagship pin. Past the threshold, more tail
# changes nothing, and a stream carrying nothing but digital zeros still decodes to the
# empty string at any tail length, so this cannot invent words the way an amplified room
# does (ADR 0021).
#
# 500 ms is 300 ms plus margin for a model neither of these measured. It is paid once, when
# a stream ends, and costs about 0.4 s of decoding at the measured real-time factor.
FLUSH_TAIL_SECONDS = 0.5

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
        self._finished = False
        self._utterances = 0
        self._silent_endpoints = 0
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
                rule1_min_trailing_silence=SILENCE_BEFORE_ANY_SPEECH_SECONDS,
                rule2_min_trailing_silence=SILENCE_AFTER_SPEECH_SECONDS,
                rule3_min_utterance_length=MAX_UTTERANCE_SECONDS,
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
                events.append(self._event(text, is_final=True, end_reason=self._why_it_ended()))
            else:
                # An endpoint with nothing decoded in it. Invisible to a caller before this
                # was counted, which made a long silence and a broken ceiling look identical
                # from the outside (ADR 0023).
                self._silent_endpoints += 1
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
        """End the stream and return any remaining hypothesis as a final.

        The stream is given `FLUSH_TAIL_SECONDS` of silence first. Without it the last word
        of every stream was truncated or lost outright, because a transducer cannot emit a
        symbol it has no future frames for — see the measurement beside that constant.

        The tail is silence this method makes up, not audio anyone spoke. It is fed to the
        decoder and never surfaces: nothing is stored, and `_audio_seconds` is deliberately
        not advanced by it, so the durations and offsets a caller reports keep describing
        the audio that actually arrived.

        Calling this twice returns nothing the second time. A closed stream cannot accept
        the tail, and re-reading the hypothesis would emit the same utterance again and
        count it twice. `reset()` reopens the stream and clears the flag.
        """
        if self._recognizer is None or self._stream is None or self._finished:
            return ()
        self._finished = True

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover - numpy arrives with the stack
            raise StreamingRecognitionError(f"numpy is required: {exc}") from exc

        recognizer = self._recognizer
        stream = self._stream
        tail = numpy.zeros(int(REQUIRED_SAMPLE_RATE_HZ * FLUSH_TAIL_SECONDS), dtype=numpy.float32)
        stream.accept_waveform(REQUIRED_SAMPLE_RATE_HZ, tail)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        text = recognizer.get_result(stream).strip()
        if not text:
            return ()

        self._utterances += 1
        return (self._event(text, is_final=True, end_reason=EndReason.FLUSH),)

    def reset(self) -> None:
        """Discard in-flight state. The loaded model is kept; reloading costs seconds."""
        if self._recognizer is not None:
            self._stream = self._recognizer.create_stream()
        self._finished = False
        self._utterances = 0
        self._silent_endpoints = 0
        self._last_partial = ""
        self._audio_seconds = 0.0
        self._utterance_started_at = 0.0

    def _event(
        self, text: str, *, is_final: bool, end_reason: EndReason | None = None
    ) -> TranscriptEvent:
        duration = self._audio_seconds - self._utterance_started_at
        return TranscriptEvent(
            utterance_index=max(1, self._utterances),
            text=text,
            is_final=is_final,
            audio_offset_seconds=self._utterance_started_at,
            # Streaming latency is not "time to run an inference" — the work happened as
            # the audio arrived. What a caller cares about is how far behind the audio the
            # text is, which for a partial is essentially nothing.
            latency_seconds=0.0,
            duration_seconds=duration if is_final else None,
            end_reason=end_reason,
        )

    def _why_it_ended(self) -> EndReason:
        """Which rule stopped the utterance.

        sherpa reports only *that* an endpoint occurred, not which of its three rules fired,
        so this is inferred from the one thing that distinguishes them: rule 3 is a clock and
        triggers exactly at the ceiling, while the silence rules can only trigger before it.
        Inferred rather than reported, and named that way, because a measurement whose
        provenance is a guess should say so.
        """
        length = self._audio_seconds - self._utterance_started_at
        if length >= MAX_UTTERANCE_SECONDS - _CEILING_TOLERANCE_SECONDS:
            return EndReason.MAX_DURATION
        return EndReason.SILENCE

    @property
    def silent_endpoints(self) -> int:
        """Endpoints that decoded no text. `OPERATIONAL_METADATA`: a count.

        The number that tells a long silence apart from an endpointer that is not firing.
        """
        return self._silent_endpoints

    def validate_format(self, audio_format: AudioFormat) -> None:
        """Refuse audio the model was not trained on, rather than resampling it."""
        if audio_format.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise StreamingRecognitionError(
                f"this model expects {REQUIRED_SAMPLE_RATE_HZ} Hz audio but was given "
                f"{audio_format.sample_rate_hz} Hz. Resample deliberately upstream."
            )
