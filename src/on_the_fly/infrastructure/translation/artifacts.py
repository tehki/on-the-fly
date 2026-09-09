"""Which translation models may be loaded, and how their bytes are trusted (ADR 0009).

`ModelStore` pins Hugging Face repositories by revision and per-file digest. This module
exists because the artefact adopted here is not a Hugging Face repository: it is the
`.zip` OPUS-MT publishes itself, and it is pinned by URL and digest instead.

**The pin covers what the publisher published. The converted model is a derived cache.**
That is ADR 0009's rule, and it is the answer to the objection ADR 0007 raised about Tajik:
a digest over a locally converted file attests to the machine that converted it and to
nothing else. Here the trust comes from the zip — bytes that demonstrably came from
Helsinki-NLP — and the CTranslate2 directory is a build product derived from verified
input. It is never verified against a stored digest, because there is no publisher whose
digest that would be.

Conversion is cheap enough for this to be honest rather than a rationalisation: 5.5 s on
the reference machine, using `OpusMTConverter`, which needs neither `transformers` nor
`torch`. That is what makes the route viable on an end user's machine at all — the
Hugging Face checkpoint of the same model converts only through
`ct2-transformers-converter`, which drags in torch, and putting torch on every user's
machine is not something a local-first desktop application can do.

**Licence.** The archive ships its own `LICENSE`: Creative Commons **Attribution 4.0**.
The Hugging Face mirror of the same model declares `apache-2.0`. They disagree, and this
project follows the licence that travels inside the artefact it actually loads. CC-BY-4.0
permits commercial use and requires attribution, so the notice in `ATTRIBUTION` below is a
product obligation, not a formality: it has to be reachable by a user, not merely present
in a source file.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


class TranslationArtifactError(RuntimeError):
    """An artefact could not be obtained, verified, or converted."""


class ArtifactIntegrityError(TranslationArtifactError):
    """A downloaded artefact did not match its pinned digest."""


class ArtifactNotPresentError(TranslationArtifactError):
    """The artefact is absent and downloading was not enabled."""


@dataclass(frozen=True)
class MarianArtifact:
    """A pinned OPUS-MT release: one URL, one digest, one direction.

    `sha256` is of the published `.zip` exactly as served. It was taken from a download
    whose byte count matched the `content-length` the server advertised, and the archive
    has not changed since February 2020.
    """

    name: str
    url: str
    sha256: str
    source_language: str
    target_language: str
    licence: str
    attribution: str
    members: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if len(self.sha256) != 64:
            raise ValueError(f"digest for {self.name!r} is not a SHA-256 hex string")
        if not self.url.startswith("https://"):
            # Not a style preference. An artefact fetched over plaintext has no integrity
            # story before the digest check, and the digest is what the check compares to.
            raise ValueError(f"artefact {self.name!r} must be fetched over https")

    @property
    def pair(self) -> tuple[str, str]:
        return (self.source_language, self.target_language)

    def __str__(self) -> str:
        return f"{self.name} ({self.source_language}->{self.target_language}, {self.licence})"


# Helsinki-NLP's own release. Verified 2026-09-04: HTTP 200, content-length 284142010,
# last modified 2020-02-14, and the downloaded bytes matched that length exactly.
#
# `members` is every file needed to convert and run, and nothing else. The archive also
# contains training logs and the publisher's `preprocess.sh` / `postprocess.sh`, which are
# shell scripts carrying hard-coded paths from the publisher's own cluster. They are
# untrusted external content under Article 4 and are read, never executed; what they
# specify (unicode punctuation replacement, whitespace squeeze, sentencepiece encode) is
# implemented in `opus_mt.py` instead.
OPUS_MT_EN_RU = MarianArtifact(
    name="opus-mt-en-ru",
    url="https://object.pouta.csc.fi/OPUS-MT-models/en-ru/opus-2020-02-11.zip",
    sha256="798027c7e4ae7ddf89fea13ce80de517b6726d7e710fa5a9b5a376316dbf1677",
    source_language="en",
    target_language="ru",
    licence="CC-BY-4.0",
    attribution=(
        "English-Russian translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-11, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)

# The other direction, so two people can hold a conversation rather than one being
# understood. Verified 2026-09-04: HTTP 200, 284239842 bytes, and the same CC-BY-4.0
# LICENSE travelling inside the archive.
#
# This pair publishes **two** releases, 2020-02-11 and 2020-02-26. The later one is taken:
# on the publisher's own evaluation it scores BLEU 61.1 against 60.8 and chrF2 0.736
# against 0.734. A small margin, but there is no reason to pin the worse of two artefacts,
# and "the OPUS-MT model for ru-en" is not a well-defined phrase — which is the argument
# for pinning an exact URL rather than a name.
OPUS_MT_RU_EN = MarianArtifact(
    name="opus-mt-ru-en",
    url="https://object.pouta.csc.fi/OPUS-MT-models/ru-en/opus-2020-02-26.zip",
    sha256="0dec3116acadd21b647cbcf8455a5bea22e5242e82633c7551e4c4767e799e83",
    source_language="ru",
    target_language="en",
    licence="CC-BY-4.0",
    attribution=(
        "Russian-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)

# French, both directions (ADR 0032). Each pair publishes two releases, and choosing between
# them took two rounds — the publisher's scores, and then a question the scores cannot see.
#
# Averaged over the twenty test sets both releases of a direction were re-evaluated on:
#
#     en-fr   opus-2019-12-04  chrF2 0.63654   wins  4 of 20
#             opus-2020-02-26  chrF2 0.63899   wins 16 of 20   <- taken
#     fr-en   opus-2019-12-05  chrF2 0.63359   wins 15 of 20   <- rejected anyway
#             opus-2020-02-26  chrF2 0.63247   wins  5 of 20   <- taken
#
# **The fr-en release that scores better cannot be loaded here.** Its own manifest says
# `pre-processing: normalization + tokenization + BPE`, and it ships `source.bpe` and
# `target.bpe` where every other artefact in this file ships `source.spm` and `target.spm`.
# `opus_mt.py` tokenises with sentencepiece; loading that release means admitting a BPE
# implementation under Article 12 to buy 0.11 chrF2. It is not worth a dependency, and it
# would not have been visible from the scores alone.
#
# ADR 0009's rule was already "pin an exact URL, because 'the OPUS-MT model for this pair'
# is not a well-defined phrase". This is what that looks like when two releases of one pair
# are not even the same kind of artefact.
#
# Verified 2026-09-06: both HTTP 200, last modified 2020-02-26, and the downloaded bytes
# matched the advertised content-length exactly — 278391154 for en-fr and 278208212 for
# fr-en. That check is not a formality. A first attempt at en-fr was cut off by a timeout at
# 191651840 bytes and produced a perfectly well-formed SHA-256 of a truncated file, and a
# second attempt at fr-en was corrupted to 290024502 bytes — larger than the real artefact —
# by two downloads writing to one path.
OPUS_MT_EN_FR = MarianArtifact(
    name="opus-mt-en-fr",
    url="https://object.pouta.csc.fi/OPUS-MT-models/en-fr/opus-2020-02-26.zip",
    sha256="fd2431cea589bf136f11a7428f6f84ba960ce42c3e3f5009deef9e10a766d395",
    source_language="en",
    target_language="fr",
    licence="CC-BY-4.0",
    attribution=(
        "English-French translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)

OPUS_MT_FR_EN = MarianArtifact(
    name="opus-mt-fr-en",
    url="https://object.pouta.csc.fi/OPUS-MT-models/fr-en/opus-2020-02-26.zip",
    sha256="9bfe937fd2bf1467b6a31ab1730870d4057bd171bd2c5d8083af5528cadbe770",
    source_language="fr",
    target_language="en",
    licence="CC-BY-4.0",
    attribution=(
        "French-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)

# German, the first pair whose *source* this project cannot stream (ADR 0038). German
# recognition is the batch tier — Whisper, an utterance at a time — and translation does not
# care: a target needs a translation model and nothing else, so `en->de` works from a live
# microphone today while `de->en` works from a file.
#
# The release is chosen the same way the others were, and the choice was not free. `de-en`
# publishes three: 2019-12-04, 2019-12-18 and 2020-02-26. Only the last is sentencepiece —
# the publisher's own `.yml` records the first two as `pre-processing: normalization +
# tokenization + BPE` — and ADR 0032 already refused a BPE release rather than admit a second
# tokeniser to every user's machine. Verified 2026-09-09: HTTP 200, content-length 275152328,
# last modified 2020-02-26, CC-BY-4.0 travelling inside the archive as `LICENSE`.
OPUS_MT_DE_EN = MarianArtifact(
    name="opus-mt-de-en",
    url="https://object.pouta.csc.fi/OPUS-MT-models/de-en/opus-2020-02-26.zip",
    sha256="62f7cbc9aaff7630b06db951d5d83251669c35a8fc9f07966e2c6a9bf68d74f3",
    source_language="de",
    target_language="en",
    licence="CC-BY-4.0",
    attribution=(
        "German-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)

# The direction that reaches a live microphone: English, Russian or French spoken now, read
# in German. Verified 2026-09-09: HTTP 200, content-length 275458389, last modified
# 2020-02-26, the same CC-BY-4.0 `LICENSE` inside. Same three releases, same reason for
# taking the 2020 one.
OPUS_MT_EN_DE = MarianArtifact(
    name="opus-mt-en-de",
    url="https://object.pouta.csc.fi/OPUS-MT-models/en-de/opus-2020-02-26.zip",
    sha256="acd747e3b1ec32e132bb0bdd7236dff2c632a070e77f514648f293202b843b50",
    source_language="en",
    target_language="de",
    licence="CC-BY-4.0",
    attribution=(
        "English-German translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT"
    ),
    members=(
        "decoder.yml",
        "opus.spm32k-spm32k.transformer-align.model1.npz.best-perplexity.npz",
        "opus.spm32k-spm32k.vocab.yml",
        "source.spm",
        "target.spm",
        "LICENSE",
    ),
)


KNOWN_ARTIFACTS: dict[str, MarianArtifact] = {
    OPUS_MT_EN_RU.name: OPUS_MT_EN_RU,
    OPUS_MT_RU_EN.name: OPUS_MT_RU_EN,
    OPUS_MT_EN_FR.name: OPUS_MT_EN_FR,
    OPUS_MT_FR_EN.name: OPUS_MT_FR_EN,
    OPUS_MT_DE_EN.name: OPUS_MT_DE_EN,
    OPUS_MT_EN_DE.name: OPUS_MT_EN_DE,
}


def resolve(pair: tuple[str, str]) -> MarianArtifact:
    """Find the artefact serving a language pair, or refuse.

    Refusing is the point. A pair with no pinned artefact must not fall back to a model
    trained for a different direction.
    """
    for artefact in KNOWN_ARTIFACTS.values():
        if artefact.pair == pair:
            return artefact
    known = ", ".join(f"{a.source_language}->{a.target_language}" for a in KNOWN_ARTIFACTS.values())
    raise TranslationArtifactError(
        f"no pinned translation model for {pair[0]}->{pair[1]}; this project has: {known}. "
        "Adding one means pinning a published artefact and recording its licence."
    )


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TranslationModelStore:
    """Resolves a `MarianArtifact` to a converted, ready-to-load directory.

    The sequence is: fetch (only if allowed) -> verify the digest -> extract the named
    members -> convert. A failure at any step raises; there is no path that returns an
    unverified directory, which is the same contract `ModelStore.ensure` offers.
    """

    def __init__(self, cache_dir: Path | str, *, allow_download: bool = False) -> None:
        self._cache_dir = Path(cache_dir).expanduser().resolve()
        self._allow_download = allow_download

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def allow_download(self) -> bool:
        return self._allow_download

    def __repr__(self) -> str:
        return (
            f"TranslationModelStore(cache_dir={self._cache_dir!s}, "
            f"allow_download={self._allow_download})"
        )

    def archive_path(self, artefact: MarianArtifact) -> Path:
        # Keyed by digest, so two revisions of the same name never share a path and a
        # changed pin can never collide with a cached older artefact.
        return self._cache_dir / artefact.name / artefact.sha256 / "artifact.zip"

    def source_dir(self, artefact: MarianArtifact) -> Path:
        return self._cache_dir / artefact.name / artefact.sha256 / "marian"

    def converted_dir(self, artefact: MarianArtifact) -> Path:
        return self._cache_dir / artefact.name / artefact.sha256 / "ctranslate2"

    def ensure(self, artefact: MarianArtifact) -> tuple[Path, Path]:
        """Return `(converted_dir, source_dir)`, both ready to use.

        `source_dir` is returned as well because the sentencepiece models live there: the
        conversion produces the translation model, not the tokenisers.
        """
        converted = self.converted_dir(artefact)
        source = self.source_dir(artefact)
        if self._is_converted(converted) and self._is_extracted(artefact, source):
            return converted, source

        archive = self.archive_path(artefact)
        if not archive.is_file():
            if not self._allow_download:
                raise ArtifactNotPresentError(
                    f"translation model {artefact} is not in {archive.parent} and "
                    "downloading is not enabled. Fetch it explicitly with --allow-download."
                )
            self._download(artefact, archive)

        self.verify(artefact, archive)
        self._extract(artefact, archive, source)
        self._convert(source, converted)
        return converted, source

    def _is_converted(self, converted: Path) -> bool:
        return (converted / "model.bin").is_file()

    def _is_extracted(self, artefact: MarianArtifact, source: Path) -> bool:
        return source.is_dir() and all((source / name).is_file() for name in artefact.members)

    def verify(self, artefact: MarianArtifact, archive: Path) -> None:
        """Check the archive against its pin. Raises on mismatch, leaving the file alone."""
        if not archive.is_file():
            raise ArtifactIntegrityError(f"translation model {artefact} is missing at {archive}")
        actual = file_digest(archive)
        if actual != artefact.sha256:
            # Left in place, for the same reason ModelStore leaves a bad model file: a
            # mismatch may be a supply-chain event, and deleting it destroys the evidence.
            raise ArtifactIntegrityError(
                f"translation model {artefact} failed verification: expected "
                f"{artefact.sha256[:16]}..., got {actual[:16]}.... The file has been left "
                f"at {archive} for inspection and was not used."
            )

    def _download(self, artefact: MarianArtifact, archive: Path) -> None:
        from urllib.request import urlopen

        # Re-checked here and not only in `MarianArtifact.__post_init__`. `urlopen` will
        # happily open `file:` or `ftp:`, so the scheme is verified immediately before the
        # call that acts on it rather than trusting a check made at construction time
        # (Article 10.2, and the reason ruff's S310 exists).
        if not artefact.url.startswith("https://"):
            raise TranslationArtifactError(
                f"refusing to fetch {artefact} over a non-https URL: {artefact.url!r}"
            )

        archive.parent.mkdir(parents=True, exist_ok=True)
        partial = archive.with_suffix(".partial")
        try:
            # Written to a .partial name and moved only on success, so an interrupted
            # download can never be mistaken for a complete artefact by a later run.
            with (
                urlopen(artefact.url, timeout=300) as response,  # noqa: S310 - scheme checked above
                partial.open("wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
            partial.replace(archive)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise TranslationArtifactError(
                f"could not download translation model {artefact}: {exc}"
            ) from exc

    def _extract(self, artefact: MarianArtifact, archive: Path, source: Path) -> None:
        source.mkdir(parents=True, exist_ok=True)
        resolved = source.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for name in artefact.members:
                target = (resolved / name).resolve()
                if not target.is_relative_to(resolved):
                    # Zip entries are attacker-controlled names in the general case
                    # (Article 8 invariant 4). Only the pinned members are extracted, and
                    # each destination is checked even so.
                    raise TranslationArtifactError(f"archive member escapes its directory: {name}")
                with bundle.open(name) as member, target.open("wb") as handle:
                    shutil.copyfileobj(member, handle)

    def _convert(self, source: Path, converted: Path) -> None:
        try:
            from ctranslate2.converters import OpusMTConverter
        except ImportError as exc:  # pragma: no cover - exercised by the requirements install
            raise TranslationArtifactError(
                "ctranslate2 is required to convert a translation model; install the "
                f"runtime requirements. Underlying error: {exc}"
            ) from exc

        try:
            OpusMTConverter(str(source)).convert(str(converted), quantization="int8", force=True)
        except Exception as exc:
            raise TranslationArtifactError(f"could not convert translation model: {exc}") from exc
