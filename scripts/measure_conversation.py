#!/usr/bin/env python3
"""Re-derive ADR 0047's two numbers: what a conversation identifies, and what it costs.

The thirty-third measurement in `docs/PERFORMANCE_BUDGET.md` rests on two claims that a
reader cannot check by reading — *8 of 10 utterances identified correctly*, and *two
recognisers fit inside real time while three do not*. This is how both were taken, so the
next person can take them again rather than trust them.

```bash
python scripts/measure_conversation.py --languages en fr
python scripts/measure_conversation.py --languages en fr ru --cost-only
```

**Both halves are measured against the shipped `ConversationRecognizer`,** not a
reconstruction of it: identification runs the real decision and asks what it chose, and the
cost run feeds the same frames to the same recognisers in the same order the live path does.

**The cost is timed in one process, all configurations together.** That is not a convenience.
Run as separate processes on this hardware the same sweep produced 0.91x for one recogniser,
1.49x for two and 1.36x for three — an ordering that cannot be true, and is entirely the
machine's background load moving underneath the measurement. Timed together, and reported as
the best of several passes, the numbers order themselves correctly: 0.45x, 0.96x, 1.11x.

Nothing here writes a file. Audio is read from the published clips this repository already
uses for word error rates, and the transcripts are not printed — only which language won.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from on_the_fly.domain.audio import AudioFormat, TranscriptEvent  # noqa: E402
from on_the_fly.infrastructure.asr import (  # noqa: E402
    ConversationRecognizer,
    SherpaStreamingRecognizer,
    layout_for,
    resolve,
)
from on_the_fly.infrastructure.model_store import ModelStore  # noqa: E402

# The same two defaults every other measurement script in here carries.
DEFAULT_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"

# Where the published reference clips live, one directory per language, as the word error
# measurements already expect them.
DEFAULT_CLIPS = Path.home() / ".cache" / "on-the-fly" / "testwavs"

DEFAULT_FRAME_MS = 20


@dataclass(frozen=True)
class Decision:
    """One moment at which some recogniser finalised, and what was decided on it."""

    clip: str
    spoken: str
    scores: dict[str, float]
    chosen: str

    @property
    def correct(self) -> bool:
        return self.chosen == self.spoken

    @property
    def margin(self) -> float | None:
        """How far ahead the winner was, or `None` when nobody was competing.

        The distinction this measurement exists to make visible: most decisions have no
        margin at all, because the recognisers endpoint independently and one of them
        finalises alone.
        """
        if len(self.scores) < 2:
            return None
        ranked = sorted(self.scores.values(), reverse=True)
        return ranked[0] - ranked[1]


class Logging(ConversationRecognizer):
    """The shipped recogniser, saying what each decision was made on.

    A subclass rather than an instrumented copy: a measurement of a reimplementation of the
    decision would be a measurement of this file.
    """

    def __init__(self, recognisers: dict[str, object]) -> None:
        super().__init__(recognisers)
        self.decisions: list[tuple[dict[str, float], str]] = []

    def _winner(self, finals: dict[str, list[TranscriptEvent]]) -> str:
        scores = {
            code: statistics.median(
                [event.confidence for event in events if event.confidence is not None]
            )
            for code, events in finals.items()
            if events and any(event.confidence is not None for event in events)
        }
        chosen = super()._winner(finals)
        self.decisions.append((scores, chosen))
        return chosen


def frames_of(path: Path, frame_ms: int = DEFAULT_FRAME_MS) -> list[bytes]:
    """Whole frames of a WAV file, in the size the live path uses.

    A trailing partial frame is dropped rather than padded: a padded frame is audio nobody
    recorded, and the models are being asked what they heard.
    """
    with wave.open(str(path), "rb") as handle:
        audio_format = AudioFormat(
            handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        )
        size = audio_format.frame_bytes(frame_ms)
        data = handle.readframes(handle.getnframes())
    return [data[start : start + size] for start in range(0, len(data) - size + 1, size)]


def reference_clips(root: Path, languages: Sequence[str]) -> list[tuple[str, Path]]:
    """Every published clip for the languages being measured, labelled with its language.

    Only the languages under test: a clip in a third language has no correct answer here,
    because the winner can only ever be one of the models that is running.
    """
    return [
        (code, path)
        for code in languages
        if (root / code).is_dir()
        for path in sorted((root / code).glob("*.wav"))
    ]


@dataclass(frozen=True)
class Endpoint:
    """One final, and when the recogniser that produced it decided the utterance had ended.

    `at` is audio time, not wall time: two recognisers fed the same frames are compared on
    where in the audio each of them stopped, which is what a grace period would have to
    cover.
    """

    language: str
    at: float
    started: float
    duration: float


def endpoints_of(
    recognisers: dict[str, object], path: Path, frame_ms: int
) -> dict[str, list[Endpoint]]:
    """Every final each recogniser produced on one clip, with the audio time it landed at.

    Each recogniser is run alone, because the question is where *its own* endpointer fires
    rather than what the conversation decided.
    """
    found: dict[str, list[Endpoint]] = {}
    batch = frames_of(path, frame_ms)
    seconds = frame_ms / 1000
    for code, recogniser in recognisers.items():
        recogniser.reset()  # type: ignore[attr-defined]
        rows: list[Endpoint] = []
        for number, frame in enumerate(batch, 1):
            for event in recogniser.accept(frame):  # type: ignore[attr-defined]
                if event.is_final:
                    rows.append(
                        Endpoint(
                            code,
                            number * seconds,
                            event.audio_offset_seconds,
                            event.duration_seconds or 0.0,
                        )
                    )
        for event in recogniser.finish():  # type: ignore[attr-defined]
            if event.is_final:
                rows.append(
                    Endpoint(
                        code,
                        len(batch) * seconds,
                        event.audio_offset_seconds,
                        event.duration_seconds or 0.0,
                    )
                )
        found[code] = rows
    return found


def nearest_gaps(found: dict[str, list[Endpoint]], language: str) -> list[float]:
    """For each of `language`'s finals, how far away the closest other model's final was.

    The number a grace period is an answer to: an utterance whose counterpart landed 60 ms
    later could be compared by waiting, and one whose counterpart landed three seconds later
    could not be compared by any wait a caption can afford.
    """
    others = [row.at for code, rows in found.items() if code != language for row in rows]
    if not others:
        return []
    return [min(abs(row.at - other) for other in others) for row in found.get(language, [])]


def report_endpoints(
    recognisers: dict[str, object], clips: Sequence[tuple[str, Path]], frame_ms: int
) -> None:
    every: list[float] = []
    for spoken, path in clips:
        found = endpoints_of(recognisers, path, frame_ms)
        print(f"\n{path.parent.name}/{path.stem}  (spoken {spoken})")
        for code, rows in found.items():
            for row in rows:
                print(
                    f"  {code}  ended at {row.at:6.2f}s   "
                    f"utterance {row.started:6.2f}s +{row.duration:5.2f}s"
                )
        gaps = nearest_gaps(found, spoken)
        every.extend(gaps)
        if gaps:
            for row, gap in zip(found.get(spoken, []), gaps, strict=True):
                print(f"  the {spoken} final at {row.at:.2f}s: nearest other model {gap:.2f}s away")
        else:
            # ADR 0044's case: no other model said anything, so there is no distance to
            # report. Said out loud, because a clip with no gaps line and a clip whose gaps
            # were all zero would otherwise look the same.
            print("  no other model produced a final on this clip")
    if every:
        print(f"\ngaps: {', '.join(f'{gap:.2f}s' for gap in sorted(every))}")


def identify(
    recognisers: dict[str, object], clips: Sequence[tuple[str, Path]], frame_ms: int
) -> list[Decision]:
    """Run the real decision over each clip and record what it chose, utterance by utterance."""
    decisions: list[Decision] = []
    for spoken, path in clips:
        conversation = Logging(recognisers)
        conversation.reset()
        for frame in frames_of(path, frame_ms):
            conversation.accept(frame)
        conversation.finish()
        for scores, chosen in conversation.decisions:
            decisions.append(Decision(path.stem, spoken, dict(scores), chosen))
    return decisions


def report_identification(decisions: Sequence[Decision]) -> None:
    print(f"{'clip':30} {'utterance scores':40} chose  spoken")
    seen: set[str] = set()
    for decision in decisions:
        cells = " ".join(f"{code} {value:6.2f}" for code, value in sorted(decision.scores.items()))
        label = "" if decision.clip in seen else decision.clip
        seen.add(decision.clip)
        wrong = "" if decision.correct else "   <- wrong"
        print(f"{label:30} {cells:40} {decision.chosen:>5}   {decision.spoken}{wrong}")

    right = sum(1 for decision in decisions if decision.correct)
    print(f"\n{right} of {len(decisions)} utterances identified correctly")
    compared = [decision for decision in decisions if decision.margin is not None]
    print(
        f"{len(compared)} of {len(decisions)} decisions were comparisons; "
        "the rest had one model speaking alone"
    )
    for decision in compared:
        # `compared` is filtered on this, so the fallback is unreachable; it is written
        # rather than asserted because a measurement should not stop at a type narrowing.
        margin = decision.margin if decision.margin is not None else 0.0
        verdict = "correct" if decision.correct else "WRONG"
        print(f"  margin {margin:.2f} ({verdict})")


def sweep(
    groups: Sequence[Sequence[object]], material: Sequence[Sequence[bytes]], repeats: int
) -> dict[int, list[float]]:
    """Time each group over the same audio, interleaved, so they share their conditions.

    Interleaved rather than one group at a time: the load on this machine drifts over
    minutes, and a sweep that runs all of one configuration before starting the next
    measures that drift as though it were the configuration.
    """
    times: dict[int, list[float]] = {len(group): [] for group in groups}
    for _ in range(repeats):
        for group in groups:
            started = time.monotonic()
            for batch in material:
                for recogniser in group:
                    recogniser.reset()  # type: ignore[attr-defined]
                for frame in batch:
                    for recogniser in group:
                        recogniser.accept(frame)  # type: ignore[attr-defined]
                for recogniser in group:
                    recogniser.finish()  # type: ignore[attr-defined]
            times[len(group)].append(time.monotonic() - started)
    return times


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--languages", nargs="+", default=["en", "fr"])
    parser.add_argument("--clips", type=Path, default=DEFAULT_CLIPS)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cost-only", action="store_true")
    parser.add_argument(
        "--endpoints",
        action="store_true",
        help="report where each model's endpointer fired instead of running the sweep",
    )
    parser.add_argument("--allow-download", action="store_true")
    return parser


def load_average() -> str:
    return Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]


def resident_mb() -> float:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def build_recognisers(
    languages: Sequence[str], cache_dir: Path, *, allow_download: bool
) -> dict[str, object]:
    store = ModelStore(cache_dir, allow_download=allow_download)
    built: dict[str, object] = {}
    print(f"baseline      {resident_mb():.0f} MB resident, load {load_average()}")
    for code in languages:
        pin = resolve(f"streaming-{code}")
        recogniser = SherpaStreamingRecognizer(store.ensure(pin), layout=layout_for(pin))
        recogniser.warm_up()
        built[code] = recogniser
        print(f"{len(built)} recogniser(s)  {resident_mb():.0f} MB after loading {pin.name}")
    return built


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.languages) < 2:
        raise SystemExit("error: a conversation needs at least two languages")
    if args.repeats < 1:
        raise SystemExit("error: --repeats must be at least 1")

    clips = reference_clips(args.clips, args.languages)
    if not clips:
        raise SystemExit(f"error: no reference clips under {args.clips}")

    recognisers = build_recognisers(
        args.languages, args.cache_dir, allow_download=args.allow_download
    )

    if args.endpoints:
        report_endpoints(recognisers, clips, args.frame_ms)
        return 0

    if not args.cost_only:
        print()
        report_identification(identify(recognisers, clips, args.frame_ms))

    material = [frames_of(path, args.frame_ms) for _, path in clips]
    audio = sum(len(batch) * args.frame_ms / 1000 for batch in material)
    order = list(recognisers.values())
    groups = [order[: count + 1] for count in range(len(order))]

    print(f"\naudio         {audio:.2f}s over {len(material)} clips, load {load_average()}")
    times = sweep(groups, material, args.repeats)
    for count in sorted(times):
        best = min(times[count])
        runs = ", ".join(f"{value:.2f}" for value in times[count])
        print(f"{count} recogniser(s): best {best:6.2f}s = {best / audio:.2f}x real time ({runs})")
    print(f"load average  {load_average()} at the end")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
