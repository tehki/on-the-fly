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
import time
from collections.abc import Sequence
from pathlib import Path

from on_the_fly.app.pipeline import (
    PipelineResult,
    StreamingRun,
    TranslatedEvent,
    run_capture,
    translate_finals,
)
from on_the_fly.domain.audio import SegmenterConfig
from on_the_fly.domain.languages import RecognitionTier
from on_the_fly.domain.languages import resolve as resolve_language
from on_the_fly.infrastructure.asr import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    STREAMING_LAYOUTS,
    FasterWhisperRecognizer,
    ModelStore,
    ModelStoreError,
    RecognitionError,
    SherpaStreamingRecognizer,
    StreamingRecognitionError,
    resolve,
)
from on_the_fly.infrastructure.audio.wav_source import WavFileSource, WavSourceError
from on_the_fly.infrastructure.translation import (
    MarianArtifact,
    TranslationArtifactError,
    TranslationError,
    TranslationModelStore,
)
from on_the_fly.infrastructure.translation import load as load_translator
from on_the_fly.infrastructure.translation import resolve as resolve_artifact

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
    segment.add_argument("--frame-ms", type=int, default=20, help="frame size (default: 20)")
    segment.add_argument(
        "--pre-roll-ms", type=int, default=300, help="audio kept before speech starts"
    )
    segment.add_argument(
        "--hangover-ms", type=int, default=500, help="silence that ends an utterance"
    )
    segment.add_argument("--min-utterance-ms", type=int, default=250)
    segment.add_argument("--max-utterance-ms", type=int, default=15_000)
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
        choices=sorted(KNOWN_MODELS),
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
    transcribe.add_argument("--frame-ms", type=int, default=20)
    transcribe.add_argument("--hangover-ms", type=int, default=500)
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
    stream.add_argument("--frame-ms", type=int, default=20)
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


def format_human(result: PipelineResult, source: WavFileSource) -> str:
    lines = [
        f"file          {source.path.name}",
        f"format        {source.audio_format.sample_rate_hz} Hz mono 16-bit",
        f"audio         {result.audio_seconds:.2f}s in {result.capture.frames_read} frames",
        "",
    ]

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
    artefact = None
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
        artefact = resolve_artifact((source_language.code, target.code))

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
            _translate_utterances(result, artefact, args.cache_dir, args.allow_download)
            if artefact is not None
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


def _translate_utterances(
    result: PipelineResult,
    artefact: MarianArtifact,
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

    converted, spm = TranslationModelStore(cache_dir, allow_download=allow_download).ensure(
        artefact
    )
    translator = load_translator(
        converted,
        spm,
        source_language=artefact.source_language,
        target_language=artefact.target_language,
    )
    print(f"translation   {artefact.name} (local, verified, {artefact.licence})")
    print(f"attribution   {artefact.attribution}")

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
                source_language=artefact.source_language,
                target_language=artefact.target_language,
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
                }
            )
        else:
            timing = f"[{record.start_seconds:7.2f}s +{record.duration_seconds:4.2f}s]"
            lines.append(f"  {timing} {text or '(nothing recognised)'}")
            rendered = (translations or {}).get(record.index)
            if rendered:
                lines.append(f"  {'':>21} {ARROW} {rendered}")

    if as_json:
        return json.dumps(
            {
                "file": source.path.name,
                "model": model_name,
                "audio_seconds": round(result.audio_seconds, 3),
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
        "",
    ]
    footer = [
        "",
        f"wall time     {result.wall_seconds:.2f}s",
        f"recognition   {recognition_total:.2f}s of that",
        f"real-time     {result.real_time_factor:.2f}x",
    ]
    body = lines or ["  (no utterances detected)"]
    return LINE_BREAK.join(header + body + footer)


def run_stream(args: argparse.Namespace) -> int:
    """Stream a file through the streaming recogniser, printing text as it appears."""
    language = resolve_language(args.language)
    if language.tier is not RecognitionTier.STREAMING:
        # Refused rather than silently downgraded. A user who asked to stream and got
        # batch latency would reasonably conclude the tool was broken.
        raise ValueError(
            f"{language.name} is not a streaming language: {language.note}. "
            f"Use 'transcribe' instead, which runs it through the batch engine."
        )

    pin_name = f"streaming-{language.code}"
    try:
        pin = resolve(pin_name)
    except KeyError:
        raise ValueError(
            f"{language.name} has no pinned streaming model yet (looked for {pin_name!r}). "
            "Pin one with scripts/pin_model.py after checking its licence."
        ) from None

    # Resolved before anything is downloaded or loaded. Asking for a pair this project
    # cannot serve should cost a message, not a 73 MB recogniser fetch first (handbook 14:
    # validate before you execute).
    target = None
    artefact = None
    if args.translate_to is not None:
        target = resolve_language(args.translate_to)
        if target.code == language.code:
            raise ValueError(f"source and target are both {target.name}; nothing to translate.")
        artefact = resolve_artifact((language.code, target.code))

    source = WavFileSource(args.path, frame_ms=args.frame_ms)
    model_dir = ModelStore(args.cache_dir, allow_download=args.allow_download).ensure(pin)
    recognizer = SherpaStreamingRecognizer(
        model_dir,
        num_threads=args.threads,
        # Which file is the encoder differs per model: the English pin names its files
        # after a training epoch, the Russian one after a chunk size (ADR 0012).
        layout=STREAMING_LAYOUTS[pin.name],
    )
    recognizer.validate_format(source.audio_format)

    # Loading is paid before the clock starts and reported on its own line. Folding it
    # into the streaming measurement would make a recogniser that keeps up comfortably
    # look like one that cannot.
    load_started = time.monotonic()
    recognizer.warm_up()
    load_seconds = time.monotonic() - load_started

    print(f"file          {source.path.name}")
    print(f"language      {language.name} ({language.code}, streaming)")
    print(f"model         {pin.name} (local, verified, {pin.licence})")
    print(f"model load    {load_seconds:.2f}s")

    translator = None
    if artefact is not None and target is not None:
        translation_store = TranslationModelStore(
            args.cache_dir, allow_download=args.allow_download
        )
        converted, spm = translation_store.ensure(artefact)
        translator = load_translator(
            converted, spm, source_language=language.code, target_language=target.code
        )
        print(f"translation   {artefact.name} (local, verified, {artefact.licence})")
        # CC-BY-4.0 requires attribution reachable by a user. This line is where that
        # obligation is met for the command line; a graphical interface owes its own.
        print(f"attribution   {artefact.attribution}")

    print()

    run = StreamingRun(source, recognizer)
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

    print()
    print(f"audio         {stats.audio_seconds:.2f}s in {stats.frames_read} frames")
    print(f"wall time     {stats.wall_seconds:.2f}s")
    pace = "keeps up" if stats.keeps_up else "TOO SLOW"
    print(f"real-time     {stats.real_time_factor:.3f}x  ({pace})  excludes model load")
    if stats.first_text_after_seconds is not None:
        print(f"first text    {stats.first_text_after_seconds:.2f}s into the audio")
    print(f"events        {stats.partials} partial, {stats.finals} final")
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
        if args.command == "gui":
            # Imported here so the command line never needs a GUI toolkit installed.
            from on_the_fly.ui.app import run as run_gui

            return run_gui()
    except (
        WavSourceError,
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
