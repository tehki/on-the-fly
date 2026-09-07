"""What this project can actually serve, as opposed to what it names.

Recognition needs a tier, a pin, *and* a file layout for that pin; translation needs an
artefact for the exact direction. This module is the one place that asks all of those at
once, so that an interface can offer only what will work.

It was written because the two disagreed. `domain/languages.py` marked all seven languages
`STREAMING` on ADR 0007's evidence that a published model existed for each — a fact about
Hugging Face rather than about this repository — while only two had a pin. A picker built
from that table offered forty-nine pairs, served two, and told the user which was which only
after they pressed Listen, by which point the window had already promised the pair it was
about to fail on.

ADR 0034 has since fixed the registry to describe what this project serves, so the tier and
the pins now agree: English, Russian and French stream, and the other four are `BATCH`
through Whisper. That makes the first two conditions in `can_recognise` belt and braces
rather than a contradiction — which is the right state for them to be in, and not a reason
to drop either. A pin added without a tier change, or the reverse, is exactly the drift this
module exists to refuse.

It refused two legs of that drift and not the third. Which files in a pinned directory are
the encoder, decoder and joiner is recorded in `STREAMING_LAYOUTS`, a separate dict from the
pins it keys, and this module never asked about it — so a pin and a tier added without a
layout left the language reported as recognisable right up to the point the recogniser was
built, where it raised a bare `KeyError`. `layout_for` refuses instead, and `can_recognise`
now asks.

It answers by asking the same resolvers every other caller asks, rather than keeping a
second list of servable pairs that could get out of step with the first. Nothing here
touches the network or loads a model: these are table lookups over pins that are already in
the source tree.
"""

from __future__ import annotations

from on_the_fly.domain.languages import SUPPORTED, Language, RecognitionTier
from on_the_fly.infrastructure.asr.models import STREAMING_PIN_PREFIX, layout_for
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
    """The pin a streaming language is served by. One naming rule, written once.

    The prefix belongs to the pin registry, which is where the pins are named, so it is
    imported rather than repeated here.
    """
    return f"{STREAMING_PIN_PREFIX}{code}"


def can_recognise(code: str) -> bool:
    """Whether live recognition of `code` is something this project can actually do.

    Three conditions: the language must be served at the streaming tier, a model for it
    must be pinned, *and* that pin must have a file layout. Since ADR 0034 the first two
    cannot disagree for any language currently in the registry, because the tier is assigned
    from the pin. All three are still checked, because the day they disagree is the day this
    matters — and refusing is the safe direction.

    The layout was the leg this module did not know about, and it is the one a contributor
    is most likely to miss: it lives in a separate dict from the pins it keys, so adding a
    pin and a tier without one left the language reported as recognisable and the recogniser
    raising a bare `KeyError` when it was chosen. Both lookups refuse with `KeyError`, so
    one clause covers them.
    """
    language = SUPPORTED.get(code.strip().lower())
    if language is None or language.tier is not RecognitionTier.STREAMING:
        return False
    try:
        layout_for(resolve_pin(streaming_pin_name(language.code)))
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
