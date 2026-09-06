#!/usr/bin/env python3
"""Measure a streaming recogniser: how fast it decodes, and how much it gets wrong.

Adopting a language means adopting a model, and ADR 0007 set the bar for that: the tier a
language is claimed at must be established from evidence before the code claims it. Two
numbers decide it.

**Real-time factor.** A streaming recogniser that decodes slower than the audio arrives is
not streaming, whatever its architecture is called. The pipeline feeds 20 ms frames and
decodes between them, so that is what this measures — not a batch decode of the whole file,
which would flatter a model that cannot keep up live.

**Word error rate.** Against a reference transcript, so the number means something. This
project treats about 15% as the line where output stops being usable (ADR 0027).

It runs against a model *directory* rather than a pinned name, because the point is to
evaluate a candidate before deciding whether to pin it. Nothing here writes audio, text or
results anywhere: it reads the wav files it is given, prints numbers, and exits.

```bash
python scripts/measure_recognition.py --model-dir ./candidate --wavs ./samples \
    --transcripts ./samples/trans.txt
```

`--transcripts` is `<id><whitespace><reference>` per line, where `<id>` matches a wav's
stem — the format the sherpa-onnx model repositories publish their own test sets in.
Without it, only the timing is reported, which is enough to reject a model that cannot keep up.
"""

from __future__ import annotations

import argparse
import sys
import time
import unicodedata
import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np

REQUIRED_SAMPLE_RATE_HZ = 16_000
FRAME_SAMPLES = 320  # 20 ms, the frame size the pipeline uses

# Above this, output stops being usable rather than merely imperfect (ADR 0027).
USABLE_WORD_ERROR_RATE = 0.15


def find_one(model_dir: Path, role: str, *, int8: bool) -> Path:
    """The encoder, decoder or joiner in a candidate directory.

    Globbed rather than named, because a candidate has not been pinned yet and every
    publisher names these files differently — after a training epoch, a chunk size, or
    nothing at all. `StreamingLayout` exists precisely so the shipping code never guesses
    this; a measurement script may, and says so.
    """
    matches = sorted(p for p in model_dir.glob(f"{role}*.onnx") if (".int8." in p.name) == int8)
    if not matches:
        precision = "int8" if int8 else "full-precision"
        raise SystemExit(f"error: no {precision} {role} in {model_dir}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise SystemExit(f"error: {len(matches)} candidate {role} files in {model_dir}: {names}")
    return matches[0]


def read_wav(path: Path) -> tuple[np.typing.NDArray[np.float32], float]:
    """Samples in [-1, 1) and the duration, or refuse the file."""
    with wave.open(str(path)) as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SystemExit(f"error: {path.name} is not mono 16-bit")
        if handle.getframerate() != REQUIRED_SAMPLE_RATE_HZ:
            raise SystemExit(
                f"error: {path.name} is {handle.getframerate()} Hz; these models need "
                f"{REQUIRED_SAMPLE_RATE_HZ} Hz. Resample deliberately rather than here."
            )
        frames = handle.getnframes()
        pcm = np.frombuffer(handle.readframes(frames), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, frames / REQUIRED_SAMPLE_RATE_HZ


def normalise(text: str) -> list[str]:
    """Words, upper-cased, stripped of punctuation but not of accents.

    Accents are kept because they are part of the word in every language this would be run
    on; folding them away would score a model as correct for output a reader would call
    wrong. Punctuation is dropped because these models emit none.
    """
    kept = [
        character
        for character in text.upper()
        if not unicodedata.category(character).startswith("P") or character == "'"
    ]
    return "".join(kept).split()


def word_errors(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Levenshtein distance over words: substitutions, deletions and insertions."""
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def load_transcripts(path: Path) -> dict[str, str]:
    """`<id><whitespace><reference>` per line, keyed by wav stem.

    Tab-separated where the publisher uses tabs and space-separated where they do not, with
    a `.wav` suffix tolerated on the id — the sherpa-onnx model repositories publish their
    test sets in both shapes, and this script exists to evaluate models from whichever
    publisher has one.
    """
    references = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        identifier, _, text = line.partition("\t") if "\t" in line else line.partition(" ")
        if not text.strip():
            raise SystemExit(
                f"error: {path.name} is not <id><whitespace><reference>: {line[:60]!r}"
            )
        references[identifier.strip().removesuffix(".wav")] = text.strip()
    return references


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_recognition",
        description="Decode wav files with a streaming model and report speed and accuracy.",
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--wavs", type=Path, required=True, help="directory of mono 16 kHz wavs")
    parser.add_argument(
        "--transcripts", type=Path, default=None, help="<id><whitespace><reference>"
    )
    parser.add_argument("--threads", type=int, default=1, help="decoder threads (default: 1)")
    parser.add_argument(
        "--full-precision",
        action="store_true",
        help="use the non-int8 export, to find out whether quantisation is the bottleneck",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.threads < 1:
        raise SystemExit("error: --threads must be at least 1")

    try:
        import sherpa_onnx
    except ImportError as exc:
        print(f"error: sherpa-onnx is not installed: {exc}", file=sys.stderr)
        return 1

    int8 = not args.full_precision
    encoder = find_one(args.model_dir, "encoder", int8=int8)
    decoder = find_one(args.model_dir, "decoder", int8=int8)
    joiner = find_one(args.model_dir, "joiner", int8=int8)
    tokens = args.model_dir / "tokens.txt"
    if not tokens.is_file():
        raise SystemExit(f"error: no tokens.txt in {args.model_dir}")

    wavs = sorted(args.wavs.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"error: no wav files in {args.wavs}")
    references = load_transcripts(args.transcripts) if args.transcripts else {}

    started = time.monotonic()
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(tokens),
        encoder=str(encoder),
        decoder=str(decoder),
        joiner=str(joiner),
        num_threads=args.threads,
        sample_rate=REQUIRED_SAMPLE_RATE_HZ,
        feature_dim=80,
        decoding_method="greedy_search",
        # Off: this measures decoding, and an endpoint mid-file would split a reference
        # transcript across two results and score the difference as errors.
        enable_endpoint_detection=False,
    )
    load_seconds = time.monotonic() - started

    print(f"model         {args.model_dir}")
    print(f"weights       {'int8' if int8 else 'full precision'}, {args.threads} thread(s)")
    print(f"encoder       {encoder.name} ({encoder.stat().st_size / 1e6:.1f} MB)")
    print(f"model load    {load_seconds:.2f}s\n")

    total_audio = 0.0
    total_decode = 0.0
    total_errors = 0
    total_words = 0

    for path in wavs:
        samples, seconds = read_wav(path)
        stream = recognizer.create_stream()
        started = time.monotonic()
        # Frame by frame, decoding between frames: what the live pipeline does. Decoding the
        # file in one call would measure a batch recogniser this is not.
        for offset in range(0, len(samples), FRAME_SAMPLES):
            frame = samples[offset : offset + FRAME_SAMPLES]
            stream.accept_waveform(REQUIRED_SAMPLE_RATE_HZ, frame)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        decode_seconds = time.monotonic() - started
        hypothesis = recognizer.get_result(stream).strip()

        total_audio += seconds
        total_decode += decode_seconds
        line = f"  {path.stem[:28]:28s} {seconds:5.2f}s  rtf {decode_seconds / seconds:5.3f}"

        reference = references.get(path.stem)
        if reference is not None:
            ref_words = normalise(reference)
            errors = word_errors(ref_words, normalise(hypothesis))
            total_errors += errors
            total_words += len(ref_words)
            line += f"  wer {errors / len(ref_words):6.1%} ({errors}/{len(ref_words)})"
        print(line)
        print(f"      {hypothesis}")
        if reference is not None:
            print(f"      ref: {reference}")

    print(f"\naudio         {total_audio:.2f}s across {len(wavs)} file(s)")
    overall_rtf = total_decode / total_audio
    verdict = "keeps up" if overall_rtf < 1.0 else "TOO SLOW - cannot stream"
    print(f"real-time     {overall_rtf:.3f}x  ({verdict})")
    if total_words:
        rate = total_errors / total_words
        margin = "usable" if rate <= USABLE_WORD_ERROR_RATE else "above the 15% usable line"
        print(f"word error    {rate:.1%}  ({total_errors}/{total_words} words, {margin})")
    else:
        print("word error    not measured - no --transcripts given")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
