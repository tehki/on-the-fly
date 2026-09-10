#!/usr/bin/env python3
"""Go and look at what the publishers offer for the pairs this project does not serve.

Three separate investigations in this repository — the `kroko` licence, Spanish, and the
`en-it` export — were somebody re-reading a decision and then checking whether the world still
matched it. Two of the three found the record had gone stale, and one of those two had been
written four days earlier. Claims about other people's repositories are the claims most likely
to rot, and re-checking them by hand is how they rot unnoticed.

So this goes and looks. For every supported language that has no pinned pair with English, it
probes the four places a pinnable model could come from and prints what it finds:

  * the OPUS-MT-models bucket, and whether the release tokenises with sentencepiece or BPE;
  * the Tatoeba-MT-models bucket, which is where the newer releases live;
  * the Hugging Face checkpoint, and whether the archive it names as its original weights
    still resolves — the link ADR 0033's chain depends on, and the one that has already been
    found dead once;
  * `onnx-community`, the only converter Article 12 has admitted, and what its export declares.

It answers nothing on its own. What it produces is the evidence a person compares against the
ADRs, which is the part that was being done from memory.

```bash
python scripts/survey_translation_models.py            # every unserved pair
python scripts/survey_translation_models.py --pair es en
```

Network-bound and therefore not in CI: a check that needs the internet to pass would fail for
reasons that are not this repository's.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from on_the_fly.domain.languages import SUPPORTED  # noqa: E402
from on_the_fly.infrastructure.translation import (  # noqa: E402
    KNOWN_ARTIFACTS,
    KNOWN_ONNX_MODELS,
)

TIMEOUT_SECONDS = 30

OPUS_MT_BUCKET = "https://object.pouta.csc.fi/OPUS-MT-models"
TATOEBA_BUCKET = "https://object.pouta.csc.fi/Tatoeba-MT-models"
HUGGING_FACE_API = "https://huggingface.co/api/models"

# The Tatoeba bucket names languages in ISO 639-3. Only the languages this project supports
# are listed, because a mapping nobody uses is a mapping nobody maintains.
THREE_LETTER = {
    "en": "eng",
    "ru": "rus",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "de": "deu",
}


@dataclass
class Finding:
    """What one source has to offer for one direction."""

    source: str
    detail: str
    usable: bool | None = None

    def render(self) -> str:
        mark = {True: "yes", False: "no ", None: "?  "}[self.usable]
        return f"    {mark} {self.source:22} {self.detail}"


@dataclass
class Survey:
    pair: tuple[str, str]
    findings: list[Finding] = field(default_factory=list)

    def add(self, source: str, detail: str, usable: bool | None = None) -> None:
        self.findings.append(Finding(source, detail, usable))


def fetch(url: str) -> bytes | None:
    """The bytes at `url`, or `None` for anything that is not a 200."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            if response.status != 200:
                return None
            return bytes(response.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def exists(url: str) -> bool:
    """Whether `url` resolves, without downloading a 300 MB archive to find out."""
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# Releases are named `opus-2020-02-26`, `opus+bt-2021-04-30`, `opusTCv20210807+bt_...-2022-03-13`
# — the date is in the name and the name does not sort by it. `opus+bt` sorts before `opus-`
# because `+` precedes `-`, so ordering these alphabetically would call the older one newest.
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def releases(bucket: str, prefix: str) -> list[str]:
    """Release names under one prefix, oldest first by the date in the name."""
    listing = fetch(f"{bucket}/?prefix={prefix}/")
    if listing is None:
        return []
    text = listing.decode("utf-8", errors="replace")
    names = {
        chunk.split("<")[0].removeprefix(f"{prefix}/").removesuffix(".zip")
        for chunk in text.split("<Key>")[1:]
        if chunk.split("<")[0].endswith(".zip") and ".eval." not in chunk.split("<")[0]
    }

    def dated(name: str) -> tuple[str, str]:
        found = _DATE.findall(name)
        # The last date in the name: `opusTCv20210807+bt_transformer-big_2022-03-13` carries
        # the corpus version first and the release date second.
        return ("-".join(found[-1]) if found else "", name)

    return sorted(names, key=dated)


def tokeniser(bucket: str, prefix: str, release: str) -> str:
    """What the publisher's own manifest says it tokenises with."""
    manifest = fetch(f"{bucket}/{prefix}/{release}.yml")
    if manifest is None:
        return "no manifest"
    for line in manifest.decode("utf-8", errors="replace").splitlines():
        if line.startswith("pre-processing:"):
            body = line.split(":", 1)[1].strip()
            return "sentencepiece" if "SentencePiece" in body else body
    return "unstated"


def hugging_face(repo: str) -> dict[str, object] | None:
    body = fetch(f"{HUGGING_FACE_API}/{repo}")
    if body is None:
        return None
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return None
    return dict(loaded) if isinstance(loaded, dict) else None


def original_weights(repo: str) -> str | None:
    """The archive a checkpoint names as the weights it was converted from."""
    body = fetch(f"https://huggingface.co/{repo}/raw/main/README.md")
    if body is None:
        return None
    for line in body.decode("utf-8", errors="replace").splitlines():
        if "download original weights" in line and "https://" in line:
            return line.split("(")[-1].rstrip(")").strip()
    return None


def survey_marian(survey: Survey, source: str, target: str) -> None:
    prefix = f"{source}-{target}"
    found = releases(OPUS_MT_BUCKET, prefix)
    if not found:
        survey.add("OPUS-MT-models", "nothing published", usable=False)
    else:
        newest = found[-1]
        kind = tokeniser(OPUS_MT_BUCKET, prefix, newest)
        survey.add(
            "OPUS-MT-models",
            f"{len(found)} release(s), newest {newest}: {kind}",
            usable=kind == "sentencepiece",
        )

    tatoeba_prefix = f"{THREE_LETTER.get(source, source)}-{THREE_LETTER.get(target, target)}"
    found = releases(TATOEBA_BUCKET, tatoeba_prefix)
    if not found:
        survey.add("Tatoeba-MT-models", "nothing published", usable=False)
    else:
        newest = found[-1]
        kind = tokeniser(TATOEBA_BUCKET, tatoeba_prefix, newest)
        survey.add(
            "Tatoeba-MT-models",
            f"{len(found)} release(s), newest {newest}: {kind}",
            usable=kind == "sentencepiece",
        )


def survey_checkpoint(survey: Survey, source: str, target: str) -> None:
    repo = f"Helsinki-NLP/opus-mt-{source}-{target}"
    if hugging_face(repo) is None:
        survey.add("HF checkpoint", f"{repo} does not exist", usable=False)
        return

    weights = original_weights(repo)
    if weights is None:
        survey.add("HF checkpoint", "exists; names no original weights", usable=None)
        return

    # The link ADR 0033's chain rests on, and the one already found dead for Spanish.
    alive = exists(weights)
    survey.add(
        "HF checkpoint",
        f"names {weights.rsplit('/', 2)[-2]}/{weights.rsplit('/', 1)[-1]}"
        f" — {'resolves' if alive else 'HTTP 404, the archive is gone'}",
        usable=alive,
    )


def survey_onnx(survey: Survey, source: str, target: str) -> None:
    repo = f"onnx-community/opus-mt-{source}-{target}"
    metadata = hugging_face(repo)
    if metadata is None:
        survey.add("onnx-community", "no export published", usable=False)
        return

    card = metadata.get("cardData") or {}
    licence = card.get("license") if isinstance(card, dict) else None
    base = card.get("base_model") if isinstance(card, dict) else None
    siblings = metadata.get("siblings")
    entries = siblings if isinstance(siblings, list) else []
    files = [str(entry.get("rfilename")) for entry in entries if isinstance(entry, dict)]
    quantised = [name for name in files if name.endswith("_int8.onnx")]
    spm = [name for name in files if name.endswith(".spm")]

    if licence is None:
        # ADR 0018 refused `Xenova` on exactly this, and ADR 0039 refused an export inside
        # this very organisation: admission is evidence about an artefact, not a namespace.
        survey.add("onnx-community", "exists and declares no licence", usable=False)
        return

    survey.add(
        "onnx-community",
        f"{licence}, base {base or 'undeclared'}, "
        f"{len(quantised)} int8 graph(s), {len(spm)} spm file(s)",
        usable=bool(licence and base and quantised and spm),
    )


def unserved_pairs() -> list[tuple[str, str]]:
    """Every direction between a supported language and English that nothing pins.

    English on one side because that is what a pin would be for: every pinned pair touches
    it, and everything else is reached through it (ADR 0037).
    """
    pinned = {artefact.pair for artefact in KNOWN_ARTIFACTS.values()}
    pinned |= {model.pair for model in KNOWN_ONNX_MODELS.values()}
    pairs: list[tuple[str, str]] = []
    for code in SUPPORTED:
        if code == "en":
            continue
        pairs.extend(pair for pair in ((code, "en"), ("en", code)) if pair not in pinned)
    return pairs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="survey_translation_models",
        description="Look at what publishers offer for the pairs this project does not serve.",
    )
    parser.add_argument(
        "--pair", nargs=2, metavar=("SOURCE", "TARGET"), help="survey one direction only"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pairs = [(args.pair[0], args.pair[1])] if args.pair else unserved_pairs()
    if not pairs:
        print("every supported language has a pinned pair with English.")
        return 0

    print(f"{len(pairs)} unserved direction(s). Probing four sources for each.\n")
    for source, target in pairs:
        survey = Survey((source, target))
        survey_marian(survey, source, target)
        survey_checkpoint(survey, source, target)
        survey_onnx(survey, source, target)

        print(f"  {source}->{target}")
        for finding in survey.findings:
            print(finding.render())
        print()

    print("Nothing here decides anything. Compare it against the ADRs that recorded a refusal;")
    print("a line that disagrees with one of them is the point of running this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
