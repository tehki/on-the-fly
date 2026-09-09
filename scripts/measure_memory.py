#!/usr/bin/env python3
"""Measure what a run actually holds in memory, against the budget that says 1200 MB.

`docs/PERFORMANCE_BUDGET.md` has carried a resident-memory target since it was written —
1200 MB, hard limit 2000 MB, *"dominated by loaded models"* — and nothing has ever measured
it. That was tolerable while a run held one recogniser and one translation model. It stopped
being tolerable when ADR 0037 made a pair reachable through **two** translation models, on an
engine chosen because it runs on constrained hardware.

So this measures the shipped command rather than a reconstruction of it: it runs
`python -m on_the_fly ...` as a subprocess and samples `VmRSS` out of `/proc`, reporting the
peak the kernel saw. What a user runs is what gets measured, including the interpreter, the
runtimes and every copy nobody meant to make.

```bash
python scripts/measure_memory.py --file audio.wav --language fr --translate-to ru
python scripts/measure_memory.py --file audio.wav --language fr --translate-to ru --engine onnx
```

**`VmHWM` is the number that matters and `VmRSS` is the one that is honest about when.** The
kernel's high-water mark cannot say *which* stage reached it, so both are reported: the peak,
and the trace of samples that shows where the steps are. A peak reached while loading is a
different problem from a peak reached while translating.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SAMPLE_INTERVAL = 0.05
# Half a second: long enough that a level is a state the run is in, not an allocation on
# its way somewhere else.
SUSTAINED_SAMPLES = 10
TARGET_MB = 1200
HARD_LIMIT_MB = 2000


def read_kb(pid: int, field: str) -> int | None:
    """One `/proc/<pid>/status` field in kB, or `None` once the process is gone.

    Returning `None` rather than raising because the race is expected: the process this is
    sampling is meant to exit, and doing so between the poll and the read is not an error.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    for line in status.splitlines():
        if line.startswith(field):
            return int(line.split()[1])
    return None


def sample_until_exit(process: subprocess.Popen[bytes]) -> tuple[list[tuple[float, float]], float]:
    """`([(seconds, MB)], peak MB)` for the life of `process`.

    The peak comes from `VmHWM`, which the kernel maintains, rather than from the maximum of
    the samples: a spike between two samples is invisible to polling and the whole point of
    asking about memory is the spike.
    """
    started = time.monotonic()
    trace: list[tuple[float, float]] = []
    while process.poll() is None:
        resident = read_kb(process.pid, "VmRSS:")
        if resident is not None:
            trace.append((time.monotonic() - started, resident / 1024.0))
        time.sleep(SAMPLE_INTERVAL)

    peak_kb = read_kb(process.pid, "VmHWM:")
    peak = peak_kb / 1024.0 if peak_kb is not None else max((mb for _, mb in trace), default=0.0)
    return trace, peak


def plateau(trace: Sequence[tuple[float, float]], hold: int = SUSTAINED_SAMPLES) -> float:
    """The highest level the run *sustained*, which is the one the budget row is about.

    Defined as the largest value that every sample in some window of `hold` consecutive
    samples exceeded — half a second at the sampling interval. That is deliberately not the
    mode: a run passes through several plateaus (before the models, after the recogniser,
    after both, after teardown) and the longest of them is usually the one where the least is
    loaded. It is also not the maximum, which is a spike that nothing has to fit inside.

    So: the highest thing held for long enough to be a state rather than an allocation.
    """
    if len(trace) < hold:
        return max((mb for _, mb in trace), default=0.0)
    levels = [mb for _, mb in trace]
    return max(min(levels[i : i + hold]) for i in range(len(levels) - hold + 1))


def verdict(megabytes: float) -> str:
    if megabytes > HARD_LIMIT_MB:
        return f"over the {HARD_LIMIT_MB} MB hard limit"
    if megabytes > TARGET_MB:
        return f"over the {TARGET_MB} MB target"
    return f"inside the {TARGET_MB} MB target, {TARGET_MB - megabytes:.0f} MB to spare"


def steps(trace: Sequence[tuple[float, float]], threshold_mb: float = 40.0) -> list[str]:
    """Where the trace jumped, which is where something was loaded.

    A model arriving is a step of hundreds of megabytes between two samples 50 ms apart, so a
    threshold this coarse finds the loads and ignores the churn.
    """
    found = []
    for (_, before), (at, after) in pairwise(trace):
        if after - before >= threshold_mb:
            found.append(f"    +{after - before:6.0f} MB at {at:5.1f}s  ->  {after:6.0f} MB")
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_memory",
        description="Run the shipped command and report the resident memory it reaches.",
    )
    parser.add_argument("--file", type=Path, required=True, help="audio to run over")
    parser.add_argument("--language", required=True)
    parser.add_argument("--translate-to", default=None)
    parser.add_argument("--engine", default=None, help="ctranslate2 (default) or onnx")
    parser.add_argument("--command", default="stream", help="stream (default) or transcribe")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--trace", action="store_true", help="print every sample, not the steps")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    command = [sys.executable, "-m", "on_the_fly", args.command, str(args.file)]
    command += ["--language", args.language]
    if args.translate_to:
        command += ["--translate-to", args.translate_to]
    if args.engine:
        command += ["--translation-engine", args.engine]
    if args.cache_dir:
        command += ["--cache-dir", str(args.cache_dir)]

    environment = {"PYTHONPATH": str(REPO_ROOT / "src")}
    print("command      ", " ".join(command[1:]))
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env={**os.environ, **environment},
    )
    trace, peak = sample_until_exit(process)
    _, errors = process.communicate()

    if process.returncode != 0:
        sys.stderr.write(errors.decode(errors="replace"))
        return process.returncode

    held = plateau(trace)
    print(f"samples       {len(trace)} at {SAMPLE_INTERVAL * 1000:.0f} ms")
    print(f"steady        {held:.0f} MB   {verdict(held)}   <- the budget row")
    print(f"peak          {peak:.0f} MB   {verdict(peak)}   <- transient, while loading")
    print(f"at exit       {trace[-1][1] if trace else 0.0:.0f} MB")
    if args.trace:
        for at, mb in trace:
            print(f"    {at:6.2f}s {mb:8.1f} MB")
    else:
        print("steps")
        for step in steps(trace):
            print(step)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
