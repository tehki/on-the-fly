"""Command line entry point: run the pipeline over a WAV file and report what happened.

```bash
python -m on_the_fly segment recording.wav
```

The output is metadata only — when utterances started, how long they were, why they ended.
There is no transcript because there is no recogniser yet, but the rule would hold either
way: this prints what the pipeline *did*, not what was *said*.

Deliberately small. A command line is a presentation boundary (handbook 27), so it parses
arguments, calls the composition root, and formats a result. Every decision worth making
lives above it.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from on_the_fly.app.catalogue import (
    recognisable_languages,
    servable_pairs,
    streaming_pin_name,
    translation_targets,
)
from on_the_fly.app.pipeline import (
    PipelineResult,
    StreamingRun,
    StreamingStats,
    TranslatedEvent,
    run_capture,
    translate_finals,
)
from on_the_fly.domain.audio import (
    DEFAULT_HANGOVER_MS,
    DEFAULT_MAX_UTTERANCE_MS,
    DEFAULT_MIN_UTTERANCE_MS,
    DEFAULT_PRE_ROLL_MS,
    InputQuality,
    LevelWatchingSource,
    SegmenterConfig,
    SettlingSource,
)
from on_the_fly.domain.audio.ports import Translator
from on_the_fly.domain.languages import SUPPORTED, RecognitionTier
from on_the_fly.domain.languages import resolve as resolve_language
from on_the_fly.infrastructure import parallel
from on_the_fly.infrastructure.asr import (
    DEFAULT_MODEL,
    FasterWhisperRecognizer,
    RecognitionError,
    SherpaStreamingRecognizer,
    StreamingRecognitionError,
    batch_pins,
    layout_for,
    resolve,
)
from on_the_fly.infrastructure.audio.backend import AudioDeviceError
from on_the_fly.infrastructure.audio.microphone import DEFAULT_FRAME_MS, MicrophoneSource
from on_the_fly.infrastructure.audio.wav_source import WavFileSource, WavSourceError
from on_the_fly.infrastructure.model_store import ModelStore, ModelStoreError
from on_the_fly.infrastructure.translation import (
    DEFAULT_ENGINE,
    TranslationArtifactError,
    TranslationChoice,
    TranslationEngine,
    TranslationError,
    open_translator,
)
from on_the_fly.infrastructure.translation import resolve_engine as resolve_artifact

# Models are DURABLE_PROJECT_ARTIFACT, not project content: intentionally persistent, and
# carrying nothing anyone said. They live outside the repository so a checkout stays small.
DEFAULT_MODEL_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"

LINE_BREAK = chr(10)
# Marks a translated line under the transcript it came from.
ARROW = chr(8594)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_RETENTION_FAILURE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="on-the-fly",
        description="Segment speech from a WAV file. Reports metadata only.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    segment = subcommands.add_parser(
        "segment", help="split a WAV file into utterances and report on them"
    )
    segment.add_argument("path", type=Path, help="path to a mono 16-bit WAV file")
    segment.add_argument(
        "--frame-ms",
        type=int,
        default=DEFAULT_FRAME_MS,
        help=f"frame size (default: {DEFAULT_FRAME_MS})",
    )
    segment.add_argument(
        "--pre-roll-ms",
        type=int,
        default=DEFAULT_PRE_ROLL_MS,
        help="audio kept before speech starts",
    )
    segment.add_argument(
        "--hangover-ms",
        type=int,
        default=DEFAULT_HANGOVER_MS,
        help="silence that ends an utterance",
    )
    segment.add_argument("--min-utterance-ms", type=int, default=DEFAULT_MIN_UTTERANCE_MS)
    segment.add_argument("--max-utterance-ms", type=int, default=DEFAULT_MAX_UTTERANCE_MS)
    segment.add_argument(
        "--allowed-root",
        type=Path,
        default=None,
        help="confine the input path to this directory",
    )
    segment.add_argument("--json", action="store_true", help="emit machine-readable output")

    transcribe = subcommands.add_parser(
        "transcribe",
        help="segment a WAV file and transcribe each utterance with a local model",
        description=(
            "Transcribes on your machine with a pinned, integrity-verified model. "
            "The text is shown to you and is not retained by this program - but if you "
            "redirect this output to a file, that file is yours to look after."
        ),
    )
    transcribe.add_argument("path", type=Path, help="path to a mono 16 kHz WAV file")
    transcribe.add_argument(
        "--model",
        default=DEFAULT_MODEL.name,
        # The batch pins, not every pin. A streaming model here was an accepted argument
        # that fetched 73 MB and then failed inside CTranslate2 (ADR 0041).
        choices=sorted(batch_pins()),
        help=f"pinned model to use (default: {DEFAULT_MODEL.name})",
    )
    transcribe.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_MODEL_CACHE, help="where models are stored"
    )
    transcribe.add_argument(
        "--allow-download",
        action="store_true",
        help="permit fetching the model if it is not already present (off by default)",
    )
    transcribe.add_argument("--language", default=None, help="force a language, e.g. en")
    transcribe.add_argument(
        "--translate-to",
        default=None,
        metavar="LANG",
        help=(
            "translate each utterance into this language, e.g. en. Requires --language so "
            "the pair is explicit; only pairs with a pinned model are accepted"
        ),
    )
    transcribe.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
        help=(
            "which runtime executes the translation model (default: ctranslate2, which is "
            "faster). 'onnx' is the engine that runs on mobile hardware (ADR 0018)"
        ),
    )
    transcribe.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    transcribe.add_argument("--hangover-ms", type=int, default=DEFAULT_HANGOVER_MS)
    transcribe.add_argument("--json", action="store_true")

    stream = subcommands.add_parser(
        "stream",
        help="transcribe a WAV file with the streaming engine, showing text as it appears",
        description=(
            "Uses a streaming model, so text appears while the speaker is still talking. "
            "Partial results may be replaced as the model revises them. Only languages "
            "with a pinned streaming model can be used; the rest run through 'transcribe'."
        ),
    )
    stream.add_argument("path", type=Path, help="path to a mono 16 kHz WAV file")
    stream.add_argument(
        "--language",
        default="en",
        help="language to recognise (default: en). Only streaming-tier languages are accepted",
    )
    stream.add_argument("--cache-dir", type=Path, default=DEFAULT_MODEL_CACHE)
    stream.add_argument(
        "--allow-download",
        action="store_true",
        help="permit fetching the model if it is not already present (off by default)",
    )
    stream.add_argument(
        "--finals-only",
        action="store_true",
        help="hide partial results and show only finalised text",
    )
    listen = subcommands.add_parser(
        "listen",
        help="stream from a microphone, showing text as it is spoken",
        description=(
            "The same streaming pipeline as 'stream', reading a microphone instead of a "
            "file. Runs until Ctrl-C, or until --seconds have passed. Only languages with "
            "a pinned streaming model can be used."
        ),
    )
    listen.add_argument(
        "--language",
        default="en",
        help="language to recognise (default: en). Only streaming-tier languages are accepted",
    )
    listen.add_argument(
        "--translate-to",
        default=None,
        metavar="LANG",
        help=(
            "translate finalised text into this language, e.g. ru. Only pairs with a "
            "pinned model are accepted. Partials are never translated (ADR 0009)"
        ),
    )
    listen.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
        help=(
            "which runtime executes the translation model (default: ctranslate2, which is "
            "faster). 'onnx' is the engine that runs on mobile hardware (ADR 0018)"
        ),
    )
    listen.add_argument(
        "--device",
        default=None,
        help=(
            "input device, by index or name substring (default: the system default). "
            "Indices come from your platform's audio settings"
        ),
    )
    listen.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="N",
        help="stop after N seconds of capture (default: run until Ctrl-C)",
    )
    listen.add_argument("--cache-dir", type=Path, default=DEFAULT_MODEL_CACHE)
    listen.add_argument(
        "--allow-download",
        action="store_true",
        help="permit fetching the model if it is not already present (off by default)",
    )
    listen.add_argument(
        "--finals-only",
        action="store_true",
        help="hide partial results and show only finalised text",
    )
    listen.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    listen.add_argument(
        "--threads",
        type=int,
        default=1,
        help="recogniser threads (default: 1). More is slower, not faster (ADR 0014)",
    )

    languages = subcommands.add_parser(
        "languages",
        help="list what this can recognise and translate, and which pairs work end to end",
        description=(
            "What this build can actually do, derived from the pinned models rather than "
            "from a list somebody maintains. Downloads nothing and opens no device."
        ),
    )
    languages.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
        help="which engine to answer for (default: ctranslate2). Both serve the same pairs",
    )

    fetch = subcommands.add_parser(
        "fetch",
        help="download and prepare everything a language pair needs, then stop",
        description=(
            "Fetches, verifies and converts the models for one pair so a later run has "
            "nothing left to do. Useful before going offline, or on a connection where "
            "discovering the download halfway through a conversation is the wrong time."
        ),
    )
    fetch.add_argument("--language", required=True, help="the language being spoken")
    fetch.add_argument(
        "--translate-to", default=None, metavar="LANG", help="the language to translate into"
    )
    fetch.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
    )
    fetch.add_argument("--cache-dir", type=Path, default=DEFAULT_MODEL_CACHE)
    fetch.add_argument(
        "--model",
        default=DEFAULT_MODEL.name,
        choices=sorted(batch_pins()),
        help=(
            f"batch model to fetch when --language does not stream (default: {DEFAULT_MODEL.name})"
        ),
    )

    subcommands.add_parser(
        "gui",
        help="open the desktop window",
        description=(
            "Opens the desktop interface. Requires the optional UI extra: "
            "pip install -r requirements-ui.txt"
        ),
    )

    stream.add_argument(
        "--translate-to",
        default=None,
        metavar="LANG",
        help=(
            "translate finalised text into this language, e.g. ru. Only pairs with a "
            "pinned model are accepted. Partials are never translated (ADR 0009)"
        ),
    )
    stream.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
        help=(
            "which runtime executes the translation model (default: ctranslate2, which is "
            "faster). 'onnx' is the engine that runs on mobile hardware (ADR 0018)"
        ),
    )
    stream.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    stream.add_argument(
        "--threads",
        type=int,
        default=1,
        help=(
            "recogniser threads (default: 1). More is slower, not faster: measured 0.315x "
            "real time at 1 thread against 0.470x at 2 and 1.181x at 4 on four cores, with "
            "identical transcripts (ADR 0014)"
        ),
    )
    return parser


def truncation_line(source: WavFileSource) -> str | None:
    """One line when the file carried less audio than its header declared, else nothing.

    Printed next to the duration it contradicts, because every duration this project
    reports is computed from the audio that arrived and so cannot reveal what did not.
    Not an error: a half-copied recording is still worth transcribing. It just must not be
    mistaken for a whole one.
    """
    if not source.is_truncated:
        return None
    # Two measured numbers and no third derived from them. The shortfall is their
    # difference, and printing it alongside would invite a subtraction that rounding can
    # fail: a file declaring 6.625s and carrying 3.300s has lost 3.325s, which at two
    # decimals renders as 6.62, 3.30 and 3.33. A warning whose own arithmetic does not
    # add up argues against itself. `truncated_seconds` carries the figure to callers.
    return (
        f"truncated     header declares {source.declared_seconds:.2f}s but the file "
        f"carries only {source.delivered_seconds:.2f}s - audio was lost before it "
        "reached us"
    )


def format_human(result: PipelineResult, source: WavFileSource) -> str:
    lines = [
        f"file          {source.path.name}",
        f"format        {source.audio_format.sample_rate_hz} Hz mono 16-bit",
        f"audio         {result.audio_seconds:.2f}s in {result.capture.frames_read} frames",
    ]
    truncated = truncation_line(source)
    if truncated is not None:
        lines.append(truncated)
    lines.append("")

    if result.utterances:
        lines.append(f"{len(result.utterances)} utterance(s):")
        lines.extend(f"  {record}" for record in result.utterances)
    else:
        lines.append("no utterances detected")

    lines.extend(
        [
            "",
            f"wall time     {result.wall_seconds:.3f}s",
            f"real-time     {result.real_time_factor:.4f}x  (segmentation only)",
            f"invalid       {result.capture.frames_invalid} frame(s)",
        ]
    )

    # The retention line is not decoration. It is the run stating whether it left anything
    # behind, which is the one claim this project must never make without checking.
    if result.retention_clean:
        lines.append("retention     clean - nothing retained, no deletion failed")
    else:
        lines.append(
            f"retention     FAILED - {result.entries_remaining} entr(ies) remain, "
            f"{len(result.final_reap.failed)} deletion failure(s)"
        )
    return "\n".join(lines)


def format_json(result: PipelineResult, source: WavFileSource) -> str:
    return json.dumps(
        {
            "file": source.path.name,
            "sample_rate_hz": source.audio_format.sample_rate_hz,
            "audio_seconds": round(result.audio_seconds, 4),
            "declared_seconds": round(source.declared_seconds, 4),
            "truncated_seconds": round(source.truncated_seconds, 4),
            "frames_read": result.capture.frames_read,
            "frames_invalid": result.capture.frames_invalid,
            "wall_seconds": round(result.wall_seconds, 4),
            "real_time_factor": round(result.real_time_factor, 6),
            "utterances": [
                {
                    "index": record.index,
                    "start_seconds": round(record.start_seconds, 4),
                    "duration_seconds": round(record.duration_seconds, 4),
                    "frame_count": record.frame_count,
                    "ended_because": str(record.ended_because),
                }
                for record in result.utterances
            ],
            "retention_clean": result.retention_clean,
            "entries_remaining": result.entries_remaining,
        },
        indent=2,
    )


def run_segment(args: argparse.Namespace) -> int:
    config = SegmenterConfig(
        frame_ms=args.frame_ms,
        pre_roll_ms=args.pre_roll_ms,
        hangover_ms=args.hangover_ms,
        min_utterance_ms=args.min_utterance_ms,
        max_utterance_ms=args.max_utterance_ms,
    )

    source = WavFileSource(args.path, frame_ms=args.frame_ms, allowed_root=args.allowed_root)
    result = run_capture(source, config=config)

    output = format_json(result, source) if args.json else format_human(result, source)
    print(output)

    # A run that could not delete what it held is not a successful run, whatever else it
    # reported. It gets its own exit code so a script can tell the difference.
    return EXIT_OK if result.retention_clean else EXIT_RETENTION_FAILURE


def run_transcribe(args: argparse.Namespace) -> int:
    """Segment, then transcribe each utterance with a verified local model."""
    # Resolved before any model is fetched, so an unsupported pair costs a message rather
    # than a download (handbook 14).
    choice = None
    if args.translate_to is not None:
        if args.language is None:
            # Whisper will happily detect the language, but a translation model is
            # directional: the pair has to be known before the pin can be chosen, and
            # guessing it from audio would pick the model after the fact.
            raise ValueError(
                "--translate-to requires --language, because the translation model is "
                "pinned per direction and the source language decides which one."
            )
        source_language = resolve_language(args.language)
        target = resolve_language(args.translate_to)
        if target.code == source_language.code:
            raise ValueError(f"source and target are both {target.name}; nothing to translate.")
        choice = resolve_artifact((source_language.code, target.code), args.translation_engine)

    source = WavFileSource(args.path, frame_ms=args.frame_ms)
    config = SegmenterConfig(frame_ms=args.frame_ms, hangover_ms=args.hangover_ms)

    pin = resolve(args.model)
    store = ModelStore(args.cache_dir, allow_download=args.allow_download)
    # Raises unless every pinned file matched its digest. There is no path from here to an
    # unverified model.
    model_dir = store.ensure(pin)
    recognizer = FasterWhisperRecognizer(model_dir, language=args.language)

    # keep_store, so the transcripts survive long enough to be shown. This function then
    # owns the purge, and does it in a finally.
    result = run_capture(source, config=config, recognizer=recognizer, keep_store=True)
    purge_failed = False
    try:
        translations = (
            _translate_utterances(result, choice, args.cache_dir, args.allow_download)
            if choice is not None
            else None
        )
        print(
            format_transcript(
                result, source, pin.name, as_json=args.json, translations=translations
            )
        )
    finally:
        # The purge runs whatever happened above, but the exit code is decided afterwards:
        # returning from a finally would swallow whatever exception got us here.
        final = result.store.purge_all() if result.store is not None else None
        if final is not None and not final.ok:
            print(
                f"warning: {len(final.failed)} transcript(s) could not be deleted",
                file=sys.stderr,
            )
            purge_failed = True

    return EXIT_RETENTION_FAILURE if purge_failed else EXIT_OK


def _describe_translation(choice: TranslationChoice) -> None:
    """The two lines a user is owed before any translated text appears.

    CC-BY-4.0 requires attribution reachable by a user, and this is where the command line
    meets that obligation; a graphical interface owes its own.

    A bridged pair (ADR 0037) says so on its own line rather than being folded into the
    first. `name` already carries both models, but "two models are being used, and the text
    passes through a third language on its way" is a thing to be told, not to infer from a
    plus sign.
    """
    print(f"translation   {choice.name} on {choice.engine} (local, verified, {choice.licence})")
    if choice.is_pivot:
        print(f"              {choice.route}, because no single pinned model serves this pair")
    print(f"attribution   {choice.attribution}")


def _translate_utterances(
    result: PipelineResult,
    choice: TranslationChoice,
    cache_dir: Path,
    allow_download: bool,
) -> dict[int, str]:
    """Translate each recognised utterance, returning index -> translation.

    Batch latency all the way through: this path exists because Russian has no
    licence-clean streaming model (ADR 0011), not because batch is a good way to hold a
    conversation. It is the honest option rather than the fast one.

    Retention: a translation is `EPHEMERAL` the moment it exists and goes into the same
    store as the transcript it came from, so `run_transcribe`'s purge accounts for both.
    A translation that fails is skipped rather than fatal — the transcript is still worth
    showing.
    """
    store = result.store
    if store is None:  # pragma: no cover - run_capture(keep_store=True) always sets it
        return {}

    translator = open_translator(choice, cache_dir, allow_download=allow_download)
    _describe_translation(choice)

    translations: dict[int, str] = {}
    for record in result.utterances:
        if record.transcript_handle is None:
            continue
        with store.borrow(record.transcript_handle) as content:
            text = str(content)
        if not text:
            continue
        try:
            rendered = translator.translate(
                text,
                source_language=choice.source_language,
                target_language=choice.target_language,
            )
        except TranslationError:
            continue
        if rendered:
            store.put(rendered, label="translation_output")
            translations[record.index] = rendered
    return translations


def format_transcript(
    result: PipelineResult,
    source: WavFileSource,
    model_name: str,
    *,
    as_json: bool,
    translations: dict[int, str] | None = None,
) -> str:
    """Render the transcripts. This is the one place content is deliberately shown."""
    store = result.store
    lines: list[str] = []
    payload: list[dict[str, object]] = []

    for record in result.utterances:
        text = ""
        if store is not None and record.transcript_handle is not None:
            with store.borrow(record.transcript_handle) as content:
                text = str(content)
        if as_json:
            payload.append(
                {
                    "index": record.index,
                    "start_seconds": round(record.start_seconds, 3),
                    "duration_seconds": round(record.duration_seconds, 3),
                    "recognition_seconds": round(record.recognition_seconds or 0.0, 3),
                    "text": text,
                    "translation": (translations or {}).get(record.index),
                    # `None` when the recogniser does not report one, which is not the same
                    # as being sure (ADR 0042).
                    "confidence": (
                        None if record.confidence is None else round(record.confidence, 3)
                    ),
                    "failed_decode": record.decode_fell_back,
                }
            )
        else:
            timing = f"[{record.start_seconds:7.2f}s +{record.duration_seconds:4.2f}s]"
            lines.append(f"  {timing} {text or '(nothing recognised)'}")
            if record.decode_fell_back:
                # Said rather than hidden. The text is still shown, because a user who can
                # read the language is a better judge of it than a threshold — but it is the
                # difference between a transcript and something the model itself gave up on
                # (ADR 0042).
                confidence = (
                    "" if record.confidence is None else f", confidence {record.confidence:.2f}"
                )
                lines.append(
                    f"  {'':>21} ! the model rejected its own first answer here"
                    f"{confidence}; treat this as unrecognised"
                )
            rendered = (translations or {}).get(record.index)
            if rendered:
                lines.append(f"  {'':>21} {ARROW} {rendered}")

    if as_json:
        return json.dumps(
            {
                "file": source.path.name,
                "model": model_name,
                "audio_seconds": round(result.audio_seconds, 3),
                "declared_seconds": round(source.declared_seconds, 3),
                "truncated_seconds": round(source.truncated_seconds, 3),
                "wall_seconds": round(result.wall_seconds, 3),
                "real_time_factor": round(result.real_time_factor, 4),
                "utterances": payload,
            },
            indent=2,
        )

    recognition_total = sum(r.recognition_seconds or 0.0 for r in result.utterances)
    header = [
        f"file          {source.path.name}",
        f"model         {model_name} (local, verified)",
        f"audio         {result.audio_seconds:.2f}s",
    ]
    truncated = truncation_line(source)
    if truncated is not None:
        header.append(truncated)
    header.append("")
    footer = [
        "",
        f"wall time     {result.wall_seconds:.2f}s",
        f"recognition   {recognition_total:.2f}s of that",
        f"real-time     {result.real_time_factor:.2f}x",
    ]
    body = lines or ["  (no utterances detected)"]
    return LINE_BREAK.join(header + body + footer)


def resolve_streaming(args: argparse.Namespace) -> tuple[Any, Any, Any, TranslationChoice | None]:
    """`(language, pin, target, translation choice)` for a streaming command.

    Everything that can be refused is resolved here, before a device is opened or a model
    is fetched. Asking for a pair this project cannot serve should cost a message, not a
    73 MB recogniser download first (handbook 14: validate before you execute).
    """
    language = resolve_language(args.language)
    if language.tier is not RecognitionTier.STREAMING:
        # Refused rather than silently downgraded. A user who asked to stream and got batch
        # latency would reasonably conclude the tool was broken — and since ADR 0035 measured
        # what the batch engine actually returns for a non-English language, they would be
        # right about more than the latency.
        raise ValueError(
            f"{language.name} is not a streaming language: {language.note}. "
            "'transcribe' will attempt it through the batch engine, which is a fallback "
            "rather than a substitute: Whisper tiny measured 77% word error on clean read "
            "French, the nearest language anyone here has measured (ADR 0035)."
        )

    pin_name = f"streaming-{language.code}"
    try:
        pin = resolve(pin_name)
    except KeyError:
        raise ValueError(
            f"{language.name} has no pinned streaming model yet (looked for {pin_name!r}). "
            "Pin one with scripts/pin_model.py after checking its licence."
        ) from None

    target = None
    choice = None
    if args.translate_to is not None:
        target = resolve_language(args.translate_to)
        if target.code == language.code:
            raise ValueError(f"source and target are both {target.name}; nothing to translate.")
        choice = resolve_artifact((language.code, target.code), args.translation_engine)
    return language, pin, target, choice


def _load_both(
    args: argparse.Namespace,
    pin: Any,
    choice: TranslationChoice | None,
    audio_format: Any = None,
) -> tuple[SherpaStreamingRecognizer, Translator | None]:
    """The recogniser and the translation model, built at the same time.

    They do not depend on each other, and building them in series is most of the wait before
    a user can say anything: measured 2026-09-09 at 5.17 s for a direct pair on CTranslate2
    and 21.39 s for a bridged pair on ONNX, against a 3 s target and a 6 s hard limit.

    `audio_format` is checked inside the recogniser's half so that a file the model cannot
    read is still refused before anything is loaded — it is the cheap check and it stays
    first, whatever else is happening on the other thread.
    """

    def recogniser() -> SherpaStreamingRecognizer:
        directory = ModelStore(args.cache_dir, allow_download=args.allow_download).ensure(pin)
        built = SherpaStreamingRecognizer(
            directory,
            num_threads=args.threads,
            # Which file is the encoder differs per model: the English pin names its files
            # after a training epoch, the Russian one after a chunk size (ADR 0012).
            layout=layout_for(pin),
        )
        if audio_format is not None:
            built.validate_format(audio_format)
        built.warm_up()
        return built

    def translation() -> Translator | None:
        if choice is None:
            return None
        return open_translator(choice, args.cache_dir, allow_download=args.allow_download)

    return parallel.both(recogniser, translation)


# Half a second. Below this a "there was speech" claim rests on a frame or two, and accusing a
# model on that basis is worse than saying nothing (ADR 0044).
SPEECH_BEFORE_SILENCE_IS_A_FINDING_SECONDS = 0.5


def _nothing_recognised_lines(stats: StreamingStats, language: Any) -> list[str]:
    """Said when audible speech went in and no text came out.

    The one case where this project can tell a user something about the *model* rather than
    about the audio. Measured (ADR 0044): the French model produces nothing at all on English
    speech — no partials, no finals, six endpoints with nothing in them — while the level
    monitor reports `ok` throughout, and the same audio through the English model produces
    three finals. Silence, room noise and amplified noise produce no speech seconds at all, so
    they cannot reach this.

    It does not fire when the model is merely *wrong*: English audio through the French model
    is caught, and French audio through the English model comes back as confident nonsense
    with finals, which nothing here detects (ADR 0043).
    """
    if stats.finals or stats.speech_seconds < SPEECH_BEFORE_SILENCE_IS_A_FINDING_SECONDS:
        return []
    return [
        f"no text       {stats.speech_seconds:.1f}s of this audio is speech and none of it "
        "was recognised.",
        f"              If it is not {language.name}, --language is the thing to check.",
    ]


def _confidence_lines(stats: StreamingStats) -> list[str]:
    """What the recogniser thought of the run. A number, and no verdict on it.

    Per run rather than per caption, and reported rather than acted on: measured, the
    confidence of a correctly recognised hard clip overlaps the confidence of a mismatched
    model, so there is no line to draw here yet (ADR 0043). The number is printed because it
    is what a later measurement will need, and because a user comparing two runs can see it
    move.

    A recogniser that reports nothing produces no line at all, rather than a line saying
    nothing is known — which is what every run printed before this existed.
    """
    median = stats.median_confidence
    if median is None:
        return []
    return [f"confidence    {median:.2f} median over {len(stats.confidences)} final(s)"]


def run_stream(args: argparse.Namespace) -> int:
    """Stream a file through the streaming recogniser, printing text as it appears."""
    language, pin, target, choice = resolve_streaming(args)

    source = WavFileSource(args.path, frame_ms=args.frame_ms)

    # Loading is paid before the clock starts and reported on its own line. Folding it
    # into the streaming measurement would make a recogniser that keeps up comfortably
    # look like one that cannot.
    load_started = time.monotonic()
    recognizer, translator = _load_both(args, pin, choice, source.audio_format)
    load_seconds = time.monotonic() - load_started

    print(f"file          {source.path.name}")
    print(f"language      {language.name} ({language.code}, streaming)")
    print(f"model         {pin.name} (local, verified, {pin.licence})")
    print(f"model load    {load_seconds:.2f}s")

    if choice is not None:
        _describe_translation(choice)

    print()

    # Wrapped so the frames the pipeline reads are the frames that get measured. A file
    # recorded on a machine with its capture gain pinned is clipped in exactly the way a
    # live microphone is, and produces the same confident nonsense (ADR 0019).
    watched = LevelWatchingSource(source)
    run = StreamingRun(watched, recognizer)
    translation_times: list[float] = []
    translated = 0

    events = run.events()
    stream_out = (
        translate_finals(
            events,
            translator,
            source_language=language.code,
            target_language=target.code,
            store=run.store,
        )
        if translator is not None and target is not None
        else (TranslatedEvent(event) for event in events)
    )

    for item in stream_out:
        if item.is_final or not args.finals_only:
            print(f"  {item.event}")
        if item.translation is not None:
            translated += 1
            if item.translation_seconds is not None:
                translation_times.append(item.translation_seconds)
            print(f"  {'':>7}  {ARROW} {item.translation}")

    stats = run.stats
    if stats is None:  # pragma: no cover - events() always sets it
        return EXIT_FAILURE

    level = watched.overall_level
    if level.quality is not InputQuality.OK:
        # Printed before the timings, because it changes how the transcript above should be
        # read: distorted audio produces fluent words nobody said.
        print()
        print(f"input         {level}")
        print(f"              {level.quality.advice}")

    print()
    print(f"audio         {stats.audio_seconds:.2f}s in {stats.frames_read} frames")
    truncated = truncation_line(source)
    if truncated is not None:
        print(truncated)
    print(f"wall time     {stats.wall_seconds:.2f}s")
    pace = "keeps up" if stats.keeps_up else "TOO SLOW"
    print(f"real-time     {stats.real_time_factor:.3f}x  ({pace})  excludes model load")
    if stats.first_text_after_seconds is not None:
        print(f"first text    {stats.first_text_after_seconds:.2f}s into the audio")
    print(f"events        {stats.partials} partial, {stats.finals} final")
    for line in _confidence_lines(stats):
        print(line)
    for line in _nothing_recognised_lines(stats, language):
        print(line)
    if translator is not None:
        if translation_times:
            ordered = sorted(translation_times)
            median = ordered[len(ordered) // 2]
            print(
                f"translation   {translated} of {stats.finals} final(s), "
                f"median {median * 1000:.0f}ms, max {max(ordered) * 1000:.0f}ms"
            )
        else:
            # Distinguishable from "fast": nothing was translated at all. A silent zero
            # would read as success.
            print(f"translation   none produced from {stats.finals} final(s)")
    if stats.retention_clean:
        print("retention     clean - nothing retained, no deletion failed")
    else:
        print(
            f"retention     FAILED - {stats.entries_remaining} entr(ies) remain, "
            f"{len(stats.final_reap.failed)} deletion failure(s)"
        )
    return EXIT_OK if stats.retention_clean else EXIT_RETENTION_FAILURE


def parse_device(value: str | None) -> int | str | None:
    """A device index or a name substring, whichever the user gave.

    `sounddevice` accepts both, and a user reading their own audio settings has an index
    in front of them while a user reading `--help` has a name.
    """
    if value is None:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def run_listen(args: argparse.Namespace) -> int:
    """Stream from a microphone, printing text as it is spoken.

    The counterpart of `run_stream` for live audio, and the only way to exercise the
    capture path without a GUI toolkit installed. It reports what a file cannot: how much
    was discarded while the input settled (ADR 0020), and how much was dropped because the
    pipeline could not keep up.
    """
    language, pin, target, choice = resolve_streaming(args)

    load_started = time.monotonic()
    recognizer, translator = _load_both(args, pin, choice)
    load_seconds = time.monotonic() - load_started

    print(f"language      {language.name} ({language.code}, streaming)")
    print(f"model         {pin.name} (local, verified, {pin.licence})")
    print(f"model load    {load_seconds:.2f}s")

    if choice is not None:
        # The same two lines a file run prints, from the same place. This was a third copy
        # of them, and the copy did not know that a pair can be bridged (ADR 0037) — so the
        # one command that reads a live microphone was the one that did not say so.
        _describe_translation(choice)

    # The device is opened here and not before: nothing above this point needs a
    # microphone, and holding one open while validating arguments is a privacy problem
    # whether or not anything reads from it.
    microphone = MicrophoneSource(device=parse_device(args.device), frame_ms=args.frame_ms)
    # Settling under the level monitor, so the verdict is about the microphone rather than
    # about the analog path powering up (ADR 0020).
    settling = SettlingSource(microphone)
    watched = LevelWatchingSource(settling)
    recognizer.validate_format(microphone.audio_format)

    stop_timer = None
    if args.seconds is not None:
        if args.seconds <= 0:
            raise ValueError(f"--seconds must be positive, got {args.seconds}")
        # Closing the source is how a capture ends everywhere else, including the window's
        # stop button, so the timed stop takes the same path rather than inventing one.
        stop_timer = threading.Timer(args.seconds, watched.close)
        stop_timer.daemon = True
        stop_timer.start()

    run = StreamingRun(watched, recognizer)
    translation_times: list[float] = []
    translated = 0
    interrupted = False

    events = run.events()
    stream_out = (
        translate_finals(
            events,
            translator,
            source_language=language.code,
            target_language=target.code,
            store=run.store,
        )
        if translator is not None and target is not None
        else (TranslatedEvent(event) for event in events)
    )

    limit = f"{args.seconds:g}s" if args.seconds is not None else "Ctrl-C to stop"
    print()
    print(f"  listening ({limit})")
    print()

    try:
        for item in stream_out:
            if item.is_final or not args.finals_only:
                # The shape of a live utterance — how long it ran and what stopped it — is
                # the reading a recording cannot give and this command exists to take. It
                # is timings, never text (ADR 0023).
                shape = f"  ({item.event.shape})" if item.event.shape else ""
                print(f"  {item.event}{shape}")
            if item.translation is not None:
                translated += 1
                if item.translation_seconds is not None:
                    translation_times.append(item.translation_seconds)
                print(f"  {'':>7}  {ARROW} {item.translation}")
    except KeyboardInterrupt:
        # A deliberate stop, not a failure. The summary below is the point of the run.
        interrupted = True
    finally:
        if stop_timer is not None:
            stop_timer.cancel()
        # Closed explicitly rather than left to the garbage collector: this is what
        # releases the device and purges the store, and "eventually" is not a retention
        # guarantee.
        events.close()

    stats = run.stats
    if stats is None:  # pragma: no cover - events() always sets it
        return EXIT_FAILURE

    print()
    if interrupted:
        print("stopped        by Ctrl-C")
    rate = microphone.capture_rate_hz or microphone.audio_format.sample_rate_hz
    resampled = (
        f", resampled to {microphone.audio_format.sample_rate_hz} Hz"
        if microphone.is_resampling
        else ""
    )
    print(f"device        captured at {rate} Hz{resampled}")
    settled = "gave up waiting" if settling.gave_up else "before the input steadied"
    print(f"settling      {settling.discarded_ms}ms discarded, {settled}")
    print(f"audio         {stats.audio_seconds:.2f}s in {stats.frames_read} frames")
    print(f"wall time     {stats.wall_seconds:.2f}s")
    # No real-time factor: live audio arrives in real time by definition, so the ratio is
    # always about 1.0 and says nothing. Overflows are the live equivalent — they are
    # words the pipeline was too slow to receive.
    if microphone.overflow_count:
        print(f"dropped       {microphone.overflow_count} overflow(s) - audio was lost")
    else:
        print("dropped       none - nothing was lost to a slow pipeline")
    if stats.first_text_after_seconds is not None:
        print(f"first text    {stats.first_text_after_seconds:.2f}s into the audio")
    print(f"events        {stats.partials} partial, {stats.finals} final")
    for line in _confidence_lines(stats):
        print(line)
    for line in _nothing_recognised_lines(stats, language):
        print(line)
    silent = getattr(recognizer, "silent_endpoints", None)
    if silent is not None:
        # Without this, a room nobody spoke in and an endpointer that never fires produce
        # the same output: a long gap between finals and no way to tell which happened.
        print(f"endpoints     {stats.finals} with text, {silent} with none (silence)")
    if translator is not None:
        if translation_times:
            ordered = sorted(translation_times)
            median = ordered[len(ordered) // 2]
            print(
                f"translation   {translated} of {stats.finals} final(s), "
                f"median {median * 1000:.0f}ms, max {max(ordered) * 1000:.0f}ms"
            )
        else:
            print(f"translation   none produced from {stats.finals} final(s)")

    level = watched.overall_level
    print(f"input         {level}")
    if level.quality is not InputQuality.OK:
        print(f"              {level.quality.advice}")

    if stats.retention_clean:
        print("retention     clean - nothing retained, no deletion failed")
    else:
        print(
            f"retention     FAILED - {stats.entries_remaining} entr(ies) remain, "
            f"{len(stats.final_reap.failed)} deletion failure(s)"
        )
    return EXIT_OK if stats.retention_clean else EXIT_RETENTION_FAILURE


def run_languages(args: argparse.Namespace) -> int:
    """Print what this build serves, derived from the pins rather than from a list.

    A user should not have to read a README to find out whether their pair works, and a list
    maintained by hand is a list that goes stale — which is the defect ADR 0034 removed from
    the window's pickers. Everything here comes from the same resolvers the run itself will
    use moments later, so what is printed is what would happen.
    """
    engine = args.translation_engine
    streaming = recognisable_languages()
    batch = [language for language in SUPPORTED.values() if language not in streaming]

    print(f"engine        {engine}\n")
    print("recognised live   " + ", ".join(f"{lang.name} ({lang.code})" for lang in streaming))
    print("from a file only  " + ", ".join(f"{lang.name} ({lang.code})" for lang in batch))
    print(f"{'':18}through {DEFAULT_MODEL.name}, an utterance at a time")

    print("\ntranslation, by what is speaking:")
    for language in (*streaming, *batch):
        targets = translation_targets(language.code, engine=engine)
        if not targets:
            print(f"  {language.code} -> nothing is pinned to translate it")
            continue
        rendered = []
        for target in targets:
            choice = resolve_artifact((language.code, target.code), engine)
            rendered.append(f"{target.code}*" if choice.is_pivot else target.code)
        live = "live" if language in streaming else "from a file"
        print(f"  {language.code} -> {', '.join(rendered)}   ({live})")

    print("\n* reached through English, using two models rather than one (ADR 0037)")
    servable = servable_pairs(engine=engine)
    print(f"{len(servable)} pair(s) work end to end from live speech.")
    return 0


def run_fetch(args: argparse.Namespace) -> int:
    """Download, verify and convert everything a pair needs, then stop.

    The models are also *loaded*, which is slower than fetching and is the point: a pair that
    fetched but cannot be built is not ready, and finding that out here is much better than
    finding it out when somebody is already talking. The CTranslate2 route additionally
    converts on first load, which is around 13 s a model and is otherwise paid by whoever
    starts the first conversation.
    """
    language = resolve_language(args.language)
    choice = None
    if args.translate_to is not None:
        target = resolve_language(args.translate_to)
        if target.code == language.code:
            raise ValueError(f"source and target are both {target.name}; nothing to translate.")
        choice = resolve_artifact((language.code, target.code), args.translation_engine)

    if language.tier is RecognitionTier.STREAMING:
        pin = resolve(streaming_pin_name(language.code))
    else:
        pin = resolve(args.model)

    print(f"language      {language.name} ({language.code}, {language.tier})")
    print(f"model         {pin.name} ({pin.licence})")
    if choice is not None:
        print(f"translation   {choice}")
    print()

    started = time.monotonic()
    settings = argparse.Namespace(
        cache_dir=args.cache_dir, allow_download=True, threads=1, model=pin.name
    )
    if language.tier is RecognitionTier.STREAMING:
        _load_both(settings, pin, choice)
    else:
        directory = ModelStore(args.cache_dir, allow_download=True).ensure(pin)
        FasterWhisperRecognizer(directory)
        if choice is not None:
            open_translator(choice, args.cache_dir, allow_download=True)

    print(f"ready         {time.monotonic() - started:.1f}s, nothing left to download")
    print(f"cached in     {args.cache_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "segment":
            return run_segment(args)
        if args.command == "transcribe":
            return run_transcribe(args)
        if args.command == "stream":
            return run_stream(args)
        if args.command == "listen":
            return run_listen(args)
        if args.command == "languages":
            return run_languages(args)
        if args.command == "fetch":
            return run_fetch(args)
        if args.command == "gui":
            # Imported here so the command line never needs a GUI toolkit installed.
            from on_the_fly.ui.app import run as run_gui

            return run_gui()
    except (
        WavSourceError,
        AudioDeviceError,
        ModelStoreError,
        RecognitionError,
        StreamingRecognitionError,
        TranslationArtifactError,
        TranslationError,
        ValueError,
        KeyError,
    ) as exc:
        # Expected failures: an unreadable file, an unusable format, a nonsensical
        # configuration. The user gets the reason, not a traceback (handbook 48).
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
