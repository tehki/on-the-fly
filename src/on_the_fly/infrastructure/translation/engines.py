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
from on_the_fly.infrastructure import parallel
from on_the_fly.infrastructure.translation.artifacts import TranslationArtifactError
from on_the_fly.infrastructure.translation.artifacts import resolve as resolve_marian
from on_the_fly.infrastructure.translation.onnx_artifacts import resolve_onnx
from on_the_fly.infrastructure.translation.pivot import PIVOT_LANGUAGE, PivotTranslator


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
    # Set when no single artefact serves the pair and it is reached through a bridge
    # language instead (ADR 0037). `name` and `attribution` then describe both legs,
    # because CC-BY-4.0 asks for attribution and two models were used.
    via: str | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.source_language, self.target_language)

    @property
    def is_pivot(self) -> bool:
        return self.via is not None

    @property
    def route(self) -> str:
        """The pair, with the bridge in it when there is one: `fr->ru` or `fr->en->ru`."""
        hops = (self.source_language, self.via, self.target_language)
        return "->".join(hop for hop in hops if hop is not None)

    def __str__(self) -> str:
        return f"{self.name} ({self.route}, {self.licence})"


def resolve(pair: tuple[str, str], engine: TranslationEngine = DEFAULT_ENGINE) -> TranslationChoice:
    """Find the route serving `pair` on `engine`, or refuse.

    A single artefact first. Failing that, a two-leg route through `PIVOT_LANGUAGE`, which
    is how `fr<->ru` is served (ADR 0037) — both legs on the same engine, so the answer to
    "does this work on ONNX" stays one answer.

    Refusing rather than falling back to the other engine is deliberate. A caller who asked
    for ONNX because it is the engine that runs on their target hardware, and silently got
    CTranslate2, would be told the application works there when it does not. Falling back
    to a *bridge* is a different thing and is visible: the choice says so, `name` and
    `attribution` name both models, and `str()` prints the route with the bridge in it.
    """
    try:
        return _resolve_direct(pair, engine)
    except TranslationArtifactError as unserved:
        return _resolve_via_pivot(pair, engine, unserved)


def _resolve_direct(pair: tuple[str, str], engine: TranslationEngine) -> TranslationChoice:
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


def _resolve_via_pivot(
    pair: tuple[str, str], engine: TranslationEngine, unserved: TranslationArtifactError
) -> TranslationChoice:
    """Both legs through the bridge, or the refusal the caller was already owed.

    `unserved` is re-raised rather than replaced. It names the pair that was asked for and
    lists what this project pins, which is what a reader can act on; a message about
    whichever leg happened to be missing would send them to the wrong registry. A pair that
    could have been bridged and could not gets one sentence added, because there the bridge
    is the part worth explaining.
    """
    source, target = pair
    if PIVOT_LANGUAGE in pair or source == target:
        # Nothing to bridge: one side already is the bridge, so a leg would be the identity
        # and the pair is either served directly or not at all.
        raise unserved

    try:
        first = _resolve_direct((source, PIVOT_LANGUAGE), engine)
        second = _resolve_direct((PIVOT_LANGUAGE, target), engine)
    except TranslationArtifactError:
        raise TranslationArtifactError(
            f"{unserved} It cannot be reached through {PIVOT_LANGUAGE} either: that needs "
            f"both {source}->{PIVOT_LANGUAGE} and {PIVOT_LANGUAGE}->{target}."
        ) from None

    licence = (
        first.licence if first.licence == second.licence else f"{first.licence} + {second.licence}"
    )
    return TranslationChoice(
        engine=engine,
        name=f"{first.name} + {second.name}",
        licence=licence,
        # Both, in order. Two models were used and CC-BY-4.0 asks to be told about each.
        attribution=f"{first.attribution} Then: {second.attribution}",
        source_language=source,
        target_language=target,
        via=PIVOT_LANGUAGE,
    )


def open_translator(
    choice: TranslationChoice,
    cache_dir: Path | str,
    *,
    allow_download: bool = False,
    beam_size: int | None = None,
    intra_threads: int | None = None,
) -> Translator:
    """Fetch, verify and load the model `choice` names.

    **The two engines verify at different moments, and this is where that becomes visible.**
    `ModelStore.ensure` (the ONNX route) re-checks every pinned digest on every call, so a
    file altered since the last run is refused before it loads. `TranslationModelStore.ensure`
    (the CTranslate2 route) checks the publisher's `.zip` when it converts it, and afterwards
    returns the converted directory without checking anything — there is no digest to check
    it against, because the conversion is this project's own output and no publisher publishes
    one for it. What that route guarantees is that the weights it was built from matched the
    pin, and that a conversion which did not finish is not mistaken for one that did.

    Neither returns a directory it *could* verify and did not. The difference in what is
    knowable is recorded in `docs/SECURITY_PRIVACY.md` rather than flattened into one
    sentence here, because a reader who assumed the stronger guarantee applied to both would
    be wrong about the default engine.

    `beam_size` overrides the shipped decoding width and exists for one caller:
    `scripts/measure_translation.py`, which has to be able to re-ask ADR 0009's question —
    *is greedy as good as the publisher's beam 6* — per pair rather than inheriting the
    answer measured for `en<->ru`. The application never passes it, and the ONNX engine
    refuses it rather than accepting a setting it does not implement.

    `intra_threads` is there for the same reason and a sharper one: ADR 0014's answer is a
    property of a *machine*, not of this project. It found one thread about 10% slower idle
    and seven times faster with three of four cores busy, on four cores. Somebody running
    this on sixteen idle cores has a different answer, and had no way to check without
    editing the source. The application never passes this either — `DEFAULT_INTRA_THREADS`
    stays the shipped setting.
    """
    if choice.is_pivot:
        return _open_pivot(
            choice,
            cache_dir,
            allow_download=allow_download,
            beam_size=beam_size,
            intra_threads=intra_threads,
        )

    if choice.engine is TranslationEngine.ONNX:
        if beam_size is not None:
            raise ValueError(
                "beam_size is not supported on the ONNX engine, which decodes greedily. "
                "Measure beam width on CTranslate2, where it is implemented."
            )
        if intra_threads is not None:
            raise ValueError(
                "intra_threads is not supported on the ONNX engine, whose thread count is "
                "set at session creation. Measure it on CTranslate2, where ADR 0014's "
                "question was asked."
            )
        from on_the_fly.infrastructure.model_store import ModelStore
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
    extra: dict[str, int] = {}
    if beam_size is not None:
        extra["beam_size"] = beam_size
    if intra_threads is not None:
        extra["intra_threads"] = intra_threads
    return load_opus_mt(
        converted,
        spm,
        source_language=artefact.source_language,
        target_language=artefact.target_language,
        **extra,
    )


def _open_pivot(
    choice: TranslationChoice,
    cache_dir: Path | str,
    *,
    allow_download: bool,
    beam_size: int | None,
    intra_threads: int | None,
) -> Translator:
    """Load both legs and compose them.

    Both are loaded before the first translation rather than the second being opened lazily
    on first use: a pair that cannot be served should fail while the caller is still
    starting up, not midway through the first sentence somebody says.

    And both at once. The two legs do not depend on each other, and loading them in series
    made a bridged pair the slowest thing this project does — measured at 17.85 s of model
    loading on ONNX against a 6 s hard limit for being ready at all.
    """
    via = choice.via
    if via is None:  # pragma: no cover - `is_pivot` is exactly this test
        raise ValueError("a pivot choice must carry the language it bridges through")

    def leg(pair: tuple[str, str]) -> Translator:
        return open_translator(
            _resolve_direct(pair, choice.engine),
            cache_dir,
            allow_download=allow_download,
            beam_size=beam_size,
            intra_threads=intra_threads,
        )

    # At the same time rather than one after the other: neither leg needs the other, and a
    # bridged pair on ONNX otherwise pays 17.9 s of loading against a 6 s hard limit.
    first, second = parallel.both(
        lambda: leg((choice.source_language, via)),
        lambda: leg((via, choice.target_language)),
    )
    return PivotTranslator(
        first,
        second,
        source_language=choice.source_language,
        target_language=choice.target_language,
        via=via,
    )
