"""Choosing between the two translation engines, in one place (ADR 0018).

There are now two implementations of the `Translator` port: CTranslate2, which is faster
and is the desktop default, and ONNX Runtime, which is the one that can run on a phone
(ADR 0017). They load from different artefacts — a Marian archive this project converts
against a pinned Hugging Face export — and every caller that wanted a translator would
otherwise have to know both routes.

So the choice lives here and nowhere else. `resolve` answers *can this pair be served on
this engine* without touching the network; `open_translator` performs the fetch, the
verification and the load. Keeping those separate is the same fail-fast shape the rest of
the command line uses: asking for a pair nothing can serve should cost a message, not a
73 MB download followed by a message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from on_the_fly.domain.audio.ports import Translator
from on_the_fly.infrastructure.translation.artifacts import resolve as resolve_marian
from on_the_fly.infrastructure.translation.onnx_artifacts import resolve_onnx


class TranslationEngine(Enum):
    """Which runtime executes the translation model."""

    CTRANSLATE2 = "ctranslate2"
    ONNX = "onnx"

    def __str__(self) -> str:
        return self.value


DEFAULT_ENGINE = TranslationEngine.CTRANSLATE2


@dataclass(frozen=True)
class TranslationChoice:
    """A pair, an engine, and the artefact that serves them — resolved, not yet fetched.

    Carries the licence and attribution because a caller has to display them before the
    first translation appears, and because CC-BY-4.0 attribution that lives anywhere but
    beside the artefact it describes eventually describes a different artefact.
    """

    engine: TranslationEngine
    name: str
    licence: str
    attribution: str
    source_language: str
    target_language: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.source_language, self.target_language)

    def __str__(self) -> str:
        return f"{self.name} ({self.source_language}->{self.target_language}, {self.licence})"


def resolve(pair: tuple[str, str], engine: TranslationEngine = DEFAULT_ENGINE) -> TranslationChoice:
    """Find the artefact serving `pair` on `engine`, or refuse.

    Refusing rather than falling back to the other engine is deliberate. A caller who asked
    for ONNX because it is the engine that runs on their target hardware, and silently got
    CTranslate2, would be told the application works there when it does not.
    """
    if engine is TranslationEngine.ONNX:
        model = resolve_onnx(pair)
        return TranslationChoice(
            engine=engine,
            name=model.name,
            licence=model.licence,
            attribution=model.attribution,
            source_language=model.source_language,
            target_language=model.target_language,
        )

    artefact = resolve_marian(pair)
    return TranslationChoice(
        engine=engine,
        name=artefact.name,
        licence=artefact.licence,
        attribution=artefact.attribution,
        source_language=artefact.source_language,
        target_language=artefact.target_language,
    )


def open_translator(
    choice: TranslationChoice, cache_dir: Path | str, *, allow_download: bool = False
) -> Translator:
    """Fetch, verify and load the model `choice` names.

    Both branches raise rather than returning an unverified model: `ModelStore.ensure` and
    `TranslationModelStore.ensure` each check every pinned digest before anything is loaded,
    and neither has a path that returns a directory it could not verify.
    """
    if choice.engine is TranslationEngine.ONNX:
        from on_the_fly.infrastructure.asr.model_store import ModelStore
        from on_the_fly.infrastructure.translation.onnx_translator import load as load_onnx

        model = resolve_onnx(choice.pair)
        directory = ModelStore(cache_dir, allow_download=allow_download).ensure(model.pin)
        return load_onnx(
            directory,
            source_language=model.source_language,
            target_language=model.target_language,
        )

    from on_the_fly.infrastructure.translation.artifacts import TranslationModelStore
    from on_the_fly.infrastructure.translation.opus_mt import load as load_opus_mt

    artefact = resolve_marian(choice.pair)
    converted, spm = TranslationModelStore(cache_dir, allow_download=allow_download).ensure(
        artefact
    )
    return load_opus_mt(
        converted,
        spm,
        source_language=artefact.source_language,
        target_language=artefact.target_language,
    )
