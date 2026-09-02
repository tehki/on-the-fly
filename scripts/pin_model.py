#!/usr/bin/env python3
"""Produce a model pin for review.

A pin is the expected SHA-256 of every file a model needs. `ModelStore` refuses any model
that has no pin, so producing one is a deliberate act by a maintainer that lands in a
reviewed commit — never something the loader does for itself at runtime.

```bash
python scripts/pin_model.py Systran/faster-whisper-tiny --revision <sha>
```

It downloads the pinned revision to a temporary directory, digests the files, and prints a
`ModelPin` to paste into `src/on_the_fly/infrastructure/asr/models.py`.

**This is trust on first use.** The digests describe what arrived on the machine that ran
this script. That pins the model against later tampering, and against a publisher force-
pushing the tag — it does not prove the first download was the publisher's intent. Anyone
who wants a stronger guarantee should run this independently and compare, which is exactly
what a committed pin makes possible.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from on_the_fly.infrastructure.asr.model_store import (  # noqa: E402
    ModelStoreError,
    compute_digests,
)

# What a faster-whisper (CTranslate2) model directory actually needs to load.
DEFAULT_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pin_model",
        description="Download a pinned model revision and print its digests for review.",
    )
    parser.add_argument("repo_id", help="model repository, e.g. Systran/faster-whisper-tiny")
    parser.add_argument("--revision", required=True, help="git commit sha to pin")
    parser.add_argument("--name", default=None, help="short name (default: last path segment)")
    parser.add_argument("--licence", default="MIT", help="licence of the model weights")
    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=None,
        help="file to pin (repeatable; defaults to the CTranslate2 set)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    files = list(args.files or DEFAULT_FILES)
    name = args.name or args.repo_id.rsplit("/", 1)[-1]

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("error: huggingface_hub is not installed", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="otf-pin-") as workdir:
        target = Path(workdir)
        print(f"downloading {args.repo_id}@{args.revision[:12]} ...", file=sys.stderr)
        try:
            snapshot_download(
                repo_id=args.repo_id,
                revision=args.revision,
                local_dir=str(target),
                allow_patterns=files,
            )
            digests = compute_digests(target, files)
        except (ModelStoreError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        total_bytes = sum((target / f).stat().st_size for f in files)

    print(f"\n# {total_bytes / 1e6:.1f} MB across {len(files)} files")
    print("ModelPin(")
    print(f'    name="{name}",')
    print(f'    repo_id="{args.repo_id}",')
    print(f'    revision="{args.revision}",')
    print(f'    licence="{args.licence}",')
    print("    digests={")
    for filename, digest in sorted(digests.items()):
        print(f'        "{filename}": "{digest}",')
    print("    },")
    print(")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
