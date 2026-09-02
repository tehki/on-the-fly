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
from collections.abc import Sequence
from pathlib import Path

from on_the_fly.app.pipeline import PipelineResult, run_capture
from on_the_fly.domain.audio import SegmenterConfig
from on_the_fly.infrastructure.audio.wav_source import WavFileSource, WavSourceError

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "segment":
            return run_segment(args)
    except (WavSourceError, ValueError) as exc:
        # Expected failures: an unreadable file, an unusable format, a nonsensical
        # configuration. The user gets the reason, not a traceback (handbook 48).
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
