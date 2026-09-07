#!/usr/bin/env python3
"""Measure a translation pair: how close it is to a human reference, and how long it takes.

Adopting a language pair means adopting an artefact, and ADR 0009 set what has to be known
before that: what it scores against the publisher's own references, and whether it fits the
latency budget. This produces both, through the shipped translator rather than around it —
whatever `open_translator` returns is what gets measured.

**chrF2, implemented here rather than depended on.** `sacrebleu` is the usual source and is
not a dependency of this project; admitting one to compute a number that fits in forty lines
is not a trade Article 12 would approve. So the metric is written out: character n-grams up
to order 6, whitespace removed, counts accumulated across the corpus, precision and recall
averaged over orders, combined with beta 2. That is sacrebleu's `CHRF` default, and
`--validate` exists because an implementation of a metric is worthless until it reproduces a
number somebody else published.

**The publisher's `.test.txt` is four lines per record**: source, human reference, the
publisher's own hypothesis, blank. The third line is what makes `--validate` possible —
scoring our output against *their* output measures the setup, where scoring against the
reference measures the model.

```bash
python scripts/measure_translation.py --pair en ru --test-file en-ru.test.txt --limit 300
```
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from on_the_fly.infrastructure.translation import (  # noqa: E402
    DEFAULT_ENGINE,
    TranslationEngine,
    open_translator,
    resolve_engine,
)

DEFAULT_CACHE = Path.home() / ".cache" / "on-the-fly" / "models"

# sacrebleu's CHRF defaults: character n-grams to order 6, beta 2, whitespace removed.
CHAR_ORDER = 6
BETA = 2.0
_EPSILON = 1e-16


def char_ngrams(text: str, order: int) -> Counter[str]:
    """Character n-grams of one order, with whitespace removed first."""
    stripped = "".join(text.split())
    if len(stripped) < order:
        return Counter()
    return Counter(stripped[i : i + order] for i in range(len(stripped) - order + 1))


def chrf2(hypotheses: Sequence[str], references: Sequence[str]) -> float:
    """Corpus chrF2: counts accumulated across segments, then scored once.

    Accumulated rather than averaged per sentence, because a per-sentence average weights a
    three-word sentence like a thirty-word one and is a different metric with the same name.

    The F-score is computed **per n-gram order and then averaged**, which is not the same as
    averaging precision and recall and scoring once — the two differ by about 0.6 chrF2 on
    the 300-sentence set below, which is larger than the differences this script exists to
    detect. This is the order sacrebleu uses, and `--validate` is how that was established
    rather than assumed.
    """
    if len(hypotheses) != len(references):
        raise ValueError("hypotheses and references must be the same length")

    beta_squared = BETA**2
    scores: list[float] = []
    for order in range(1, CHAR_ORDER + 1):
        matches = hyp_total = ref_total = 0
        for hypothesis, reference in zip(hypotheses, references, strict=True):
            hyp_grams = char_ngrams(hypothesis, order)
            ref_grams = char_ngrams(reference, order)
            matches += sum((hyp_grams & ref_grams).values())
            hyp_total += sum(hyp_grams.values())
            ref_total += sum(ref_grams.values())
        # sacrebleu's epsilon: an order with no n-grams at all contributes ~0 rather than
        # dividing by zero or being dropped from the average.
        precision = matches / hyp_total if hyp_total else _EPSILON
        recall = matches / ref_total if ref_total else _EPSILON
        denominator = beta_squared * precision + recall
        scores.append((1 + beta_squared) * precision * recall / denominator if denominator else 0.0)

    return 100.0 * statistics.fmean(scores)


def read_test_file(path: Path, limit: int | None) -> list[tuple[str, str, str]]:
    """`(source, reference, publisher hypothesis)` per record.

    Four lines each, blank-separated, which is the shape OPUS-MT publishes. A record that
    does not have three non-empty lines is a malformed file rather than something to skip
    quietly — a silently shortened test set would change every number below.
    """
    blocks = path.read_text(encoding="utf-8").split("\n\n")
    records: list[tuple[str, str, str]] = []
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) != 3:
            raise SystemExit(
                f"error: {path.name} record {len(records) + 1} has {len(lines)} lines, "
                "expected source / reference / publisher hypothesis"
            )
        records.append((lines[0], lines[1], lines[2]))
        if limit is not None and len(records) >= limit:
            break
    if not records:
        raise SystemExit(f"error: no records in {path}")
    return records


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[index]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_translation",
        description="Translate a publisher test set and report chrF2 and latency.",
    )
    parser.add_argument("--pair", nargs=2, metavar=("SOURCE", "TARGET"), required=True)
    parser.add_argument("--test-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=300, help="sentences (default: 300)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument(
        "--engine",
        type=TranslationEngine,
        choices=list(TranslationEngine),
        default=DEFAULT_ENGINE,
    )
    parser.add_argument(
        "--beam",
        type=int,
        default=None,
        help="override the shipped decoding width, to re-ask ADR 0009's question per pair",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="override the shipped intra-op thread count, to re-ask ADR 0014's question on "
        "this machine rather than the four-core one it was answered on",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="also score against the publisher's own hypotheses, which measures the setup "
        "rather than the model",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_language, target_language = args.pair
    records = read_test_file(args.test_file, args.limit)

    choice = resolve_engine((source_language, target_language), args.engine)
    print(f"pair          {source_language}->{target_language}")
    print(f"artefact      {choice}")
    threads = args.threads or "shipped default"
    print(
        f"engine        {choice.engine}, beam {args.beam or 'shipped default'}, threads {threads}"
    )
    print(f"sentences     {len(records)} from {args.test_file.name}\n")

    started = time.monotonic()
    translator = open_translator(
        choice,
        args.cache_dir,
        allow_download=args.allow_download,
        beam_size=args.beam,
        intra_threads=args.threads,
    )
    print(f"model load    {time.monotonic() - started:.2f}s")

    hypotheses: list[str] = []
    latencies: list[float] = []
    for source, _, _ in records:
        started = time.monotonic()
        hypotheses.append(
            translator.translate(
                source, source_language=source_language, target_language=target_language
            )
        )
        latencies.append((time.monotonic() - started) * 1000.0)

    references = [reference for _, reference, _ in records]
    print(f"chrF2         {chrf2(hypotheses, references):.2f}  against human references")
    if args.validate:
        published = [hypothesis for _, _, hypothesis in records]
        print(f"  vs theirs   {chrf2(hypotheses, published):.2f}  against the publisher's output")
        print(f"  their score {chrf2(published, references):.2f}  their output vs the references")
    print(
        f"latency       p50 {percentile(latencies, 0.50):.0f}ms  "
        f"p95 {percentile(latencies, 0.95):.0f}ms  max {max(latencies):.0f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
