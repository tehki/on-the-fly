"""What this project can actually serve, as opposed to what it names.

`domain/languages.py` records the tier a language *could* be served at, and all seven of
them are `STREAMING` because a published streaming model exists for each. Whether one has
been pinned, licence-checked and committed is a different question, and only English and
Russian answer it yes (ADR 0008, ADR 0012). Translation narrows it again: two directions of
one pair (ADR 0009).

The command line asks the second question. `resolve_streaming` refuses `--language de`
before a device is opened, with a sentence that says what is missing. A picker built from
the tier asks the first, offers forty-nine pairs, serves two, and tells the user which is
which only after they press Listen — by which point the window has already promised the
pair it is about to fail on.

So this module derives the offer from the pin registries themselves. It answers by asking
the same resolvers every other caller asks, rather than keeping a second list of servable
pairs that could drift from the first. Nothing here touches the network or loads a model:
these are table lookups over pins that are already in the source tree.
"""

from __future__ import annotations

from on_the_fly.domain.languages import SUPPORTED, Language, RecognitionTier
from on_the_fly.infrastructure.asr.models import resolve as resolve_pin
from on_the_fly.infrastructure.translation.artifacts import TranslationArtifactError
from on_the_fly.infrastructure.translation.engines import (
    DEFAULT_ENGINE,
    TranslationEngine,
)
from on_the_fly.infrastructure.translation.engines import (
    resolve as resolve_engine,
)


def streaming_pin_name(code: str) -> str:
    """The pin a streaming language is served by. One naming rule, written once."""
    return f"streaming-{code}"


def can_recognise(code: str) -> bool:
    """Whether live recognition of `code` is something this project can actually do.

    Two conditions, and the second is the one that keeps being forgotten: the language must
    be served at the streaming tier, *and* a model for it must be pinned. A language can
    have a published streaming model — all seven do — without this repository having
    adopted one.
    """
    language = SUPPORTED.get(code.strip().lower())
    if language is None or language.tier is not RecognitionTier.STREAMING:
        return False
    try:
        resolve_pin(streaming_pin_name(language.code))
    except KeyError:
        return False
    return True


def recognisable_languages() -> tuple[Language, ...]:
    """The languages a live caption can be produced in, by name.

    Sorted by name rather than by code, because that is the order they are read in.
    """
    return tuple(
        language
        for language in sorted(SUPPORTED.values(), key=lambda item: item.name)
        if can_recognise(language.code)
    )


def translation_targets(
    source: str, *, engine: TranslationEngine = DEFAULT_ENGINE
) -> tuple[Language, ...]:
    """The languages `source` can be translated into on `engine`, by name.

    Asked of the engine resolver one candidate at a time rather than read off the artefact
    tables directly. Seven lookups cost nothing, and it means "servable" has exactly one
    definition in this codebase — the one `open_translator` will act on moments later.

    A target needs a translation model and nothing else: it is written, never recognised,
    so whether *it* has a streaming pin is irrelevant here.
    """
    code = source.strip().lower()
    targets = []
    for language in sorted(SUPPORTED.values(), key=lambda item: item.name):
        if language.code == code:
            continue
        try:
            resolve_engine((code, language.code), engine)
        except TranslationArtifactError:
            continue
        targets.append(language)
    return tuple(targets)


def servable_pairs(
    *, engine: TranslationEngine = DEFAULT_ENGINE
) -> tuple[tuple[Language, Language], ...]:
    """Every `(source, target)` this project can recognise *and* translate, end to end.

    The honest answer to "what does it do", in the form a reader can count.
    """
    return tuple(
        (source, target)
        for source in recognisable_languages()
        for target in translation_targets(source.code, engine=engine)
    )
