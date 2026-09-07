#!/usr/bin/env python3
"""Measure endpoint-to-caption latency, the number `docs/PERFORMANCE_BUDGET.md` governs.

That document prescribes a method — stage boundaries timed, a minimum of 50 utterances per
configuration, p50/p95/p99 rather than a mean, and the machine's background load recorded —
and until this script existed nothing performed it. Its headline, *p50 710 ms against a
700 ms target under load*, could not be re-derived by anyone, including whoever wrote it.

**What is measured.** From the moment the streaming recogniser emits a final — the endpoint,
the instant the speaker is judged to have stopped — to the moment that utterance's
translation is in hand. The source caption is on screen before this window opens; what it
bounds is how long the reader waits for the translated line, which is exactly what ADR 0009
gives up by refusing to translate partials.

It runs the shipped path: `StreamingRun` over the same `WavFileSource` the command line uses,
through `translate_finals` with the engine's own defaults. Nothing is reimplemented here, so
what it times is what a user gets.

**Fifty utterances from a short clip means repeating it**, and repeated audio is repeated —
the recogniser sees the same sentences again, so this measures the pipeline's timing rather
than its behaviour on varied speech. `--repeat` is printed in the output rather than left to
be inferred.

Timings are `OPERATIONAL_METADATA` under `docs/RETENTION_POLICY.md`: durations, a language
pair and a count. No transcript, no translation and no audio is printed or kept.

```bash
python scripts/measure_latency.py recording.wav --translate-to ru --repeat 30
```
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from on_the_fly.app.pipeline import StreamingRun, translate_finals  # noqa: E402
from on_the_fly.domain.audio import AudioFormat  # noqa: E402
from on_the_fly.infrastructure.asr.models import STREAMING_LAYOUTS  # noqa: E402
from on_the_fly.infrastructure.asr.models import resolve as resolve_pin  # noqa: E402
from on_the_fly.infrastructure.asr.sherpa_streaming import (  # noqa: E402
    SherpaStreamingRecognizer,
)
from on_the_fly.infrastructure.audio import WavFileSource  # noqa: E402
from on_the_fly.infrastructure.model_store import ModelStore  # noqa: E402
from on_the_fly.infrastructure.translation import (  # noqa: E402
    TranslationEngine,
    open_translator,
    resolve_engine,
)

DEFAULT_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"

# The budget's own rule: "Minimum 50 utterances per configuration. A single fast run is not
# evidence (handbook 64N.1)."
MINIMUM_UTTERANCES = 50

# The lines this budget is written to defend, copied from the Targets table in
# `docs/PERFORMANCE_BUDGET.md`. `tests/test_measurement_metrics.py` reads that table and
# asserts these still match it: a number duplicated out of a document drifts from it, and a
# tool that measures against a stale threshold reports the wrong verdict confidently.
#
# p95 carries both. Its hard limit was missing here until 2026-09-07, which meant this
# script could not report the breach the budget's own correction turned on — the fourth
# measurement called 1476 ms "inside the p95 target, and only just", and the fifth found a
# p95 of 2820 ms, "past the 2500 ms hard limit". The number that made that a failure rather
# than a near miss was not in the tool that performs the method.
TARGET_P50_MS = 700.0
TARGET_P95_MS = 1500.0
HARD_LIMIT_P95_MS = 2500.0
HARD_LIMIT_P99_MS = 4000.0


class RepeatingSource:
    """Plays a wav end to end `times` over, as one continuous stream.

    Wrapping rather than concatenating on disk: the frames are the ones `WavFileSource`
    produces, so the path under measurement is unchanged, and nothing is written anywhere.
    """

    def __init__(self, path: Path, *, times: int, frame_ms: int = 20) -> None:
        self._path = path
        self._times = times
        self._frame_ms = frame_ms
        probe = WavFileSource(path, frame_ms=frame_ms)
        self._format = probe.audio_format
        probe.close()

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def frames(self) -> Iterator[bytes]:
        for _ in range(self._times):
            source = WavFileSource(self._path, frame_ms=self._frame_ms)
            try:
                yield from source.frames()
            finally:
                source.close()

    def close(self) -> None:
        """Nothing to release: each pass closes its own reader as it finishes."""
        return None


def percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def background_load() -> str:
    """The machine's load average, which the budget requires be recorded.

    It is the whole difference between the two columns of the tenth measurement, and this
    project's reference machine is never idle.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:  # pragma: no cover - not every platform has it
        return "unavailable"
    return f"{one:.2f} / {five:.2f} / {fifteen:.2f} over {os.cpu_count()} cpus"


def commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:  # pragma: no cover
        return "unknown"
    return result.stdout.strip() or "unknown"


def verdict(value: float, target: float | None, hard_limit: float | None) -> str:
    """Say whether a figure met its budget, in the budget's own words.

    The script printed each measurement beside its threshold and left the comparison to the
    reader. A tool that states the verdict is one that cannot be misread in the easy
    direction, which is the direction `docs/PERFORMANCE_BUDGET.md` has already been misread
    in once.

    "past the hard limit" outranks "missed": a figure over the hard limit has also missed
    the target, and reporting the weaker of the two would understate it.
    """
    if hard_limit is not None and value > hard_limit:
        return "  PAST THE HARD LIMIT"
    if target is not None and value > target:
        return "  missed"
    return "  ok"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_latency",
        description="Endpoint-to-caption latency, measured through the shipped pipeline.",
    )
    parser.add_argument("path", type=Path, help="a mono 16 kHz WAV file")
    parser.add_argument("--language", default="en")
    parser.add_argument("--translate-to", dest="translate_to", default=None)
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="play the file this many times, to reach the 50-utterance minimum",
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument(
        "--translation-engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=None,
        help="default: the shipped engine",
    )
    return parser


def collect_latencies(
    run: StreamingRun, args: argparse.Namespace, translator: object | None
) -> list[float]:
    """Endpoint-to-caption, in milliseconds, one entry per finalised utterance."""
    events = run.events()
    latencies: list[float] = []

    if translator is None:
        # With no translator the caption is the final itself, so the window is the pipeline
        # overhead between the recogniser producing one and a caller holding it.
        previous = time.monotonic()
        for event in events:
            now = time.monotonic()
            if event.is_final:
                latencies.append((now - previous) * 1000.0)
            previous = now
        return latencies

    for item in translate_finals(
        events,
        translator,  # type: ignore[arg-type]
        source_language=args.language,
        target_language=args.translate_to,
        store=run.store,
    ):
        if item.is_final and item.translation_seconds is not None:
            latencies.append(item.translation_seconds * 1000.0)
    return latencies


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        raise SystemExit("error: --repeat must be at least 1")

    pin = resolve_pin(f"streaming-{args.language}")
    model_dir = ModelStore(args.cache_dir, allow_download=args.allow_download).ensure(pin)
    recognizer = SherpaStreamingRecognizer(model_dir, layout=STREAMING_LAYOUTS[pin.name])

    translator = None
    choice = None
    if args.translate_to is not None:
        pair = (args.language, args.translate_to)
        choice = (
            resolve_engine(pair)
            if args.translation_engine is None
            else resolve_engine(pair, args.translation_engine)
        )
        translator = open_translator(choice, args.cache_dir, allow_download=args.allow_download)

    source = RepeatingSource(args.path, times=args.repeat)
    recognizer.validate_format(source.audio_format)
    # Loading is paid before the clock starts, as everywhere else in this project.
    recognizer.warm_up()

    print(f"commit        {commit()}")
    print(f"model         {pin.name}")
    print(f"translation   {choice if choice is not None else 'none'}")
    print(f"audio         {args.path.name} x{args.repeat}")
    print(f"load average  {background_load()}\n")

    run = StreamingRun(source, recognizer)
    latencies = collect_latencies(run, args, translator)

    if not latencies:
        raise SystemExit("error: no finalised utterances; nothing to measure")

    print(f"utterances    {len(latencies)}")
    if len(latencies) < MINIMUM_UTTERANCES:
        print(
            f"              BELOW the {MINIMUM_UTTERANCES} this budget requires — raise "
            "--repeat. A single fast run is not evidence."
        )
    p50 = percentile(latencies, 0.50)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    print(
        f"endpoint->caption p50 {p50:7.0f} ms   target {TARGET_P50_MS:.0f}"
        f"{verdict(p50, TARGET_P50_MS, None)}"
    )
    print(
        f"                  p95 {p95:7.0f} ms   target {TARGET_P95_MS:.0f}"
        f"   hard {HARD_LIMIT_P95_MS:.0f}{verdict(p95, TARGET_P95_MS, HARD_LIMIT_P95_MS)}"
    )
    print(
        f"                  p99 {p99:7.0f} ms"
        f"   hard   {HARD_LIMIT_P99_MS:.0f}{verdict(p99, None, HARD_LIMIT_P99_MS)}"
    )
    print(f"                  max {max(latencies):7.0f} ms")
    # Last, and labelled: the budget's method says never the mean alone.
    print(f"                  mean{statistics.fmean(latencies):8.0f} ms   (never on its own)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
