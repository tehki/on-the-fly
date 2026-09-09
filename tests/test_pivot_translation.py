"""Translating a pair no single model serves, by going through English (ADR 0037).

French and Russian both stream and could not be translated into one another: four of the
six ordered pairs among the streaming languages worked. Direct models exist and are not
used — the only ONNX exports of them declare no licence, and ADR 0018 already refused that
publisher — so the pair is bridged through pairs this project already pins, which works on
both engines because both legs do.

The measurement that decided it is in ADR 0037. What is asserted here is the routing and
the composition: that a bridged pair is offered, that it says so, that both models are
credited, and that a pair which cannot be bridged is refused with the message it was
already owed.
"""

from __future__ import annotations

import pytest

from on_the_fly.app.cli import _describe_translation as describe_translation
from on_the_fly.infrastructure.translation import (
    KNOWN_ARTIFACTS,
    TranslationArtifactError,
    resolve_engine,
)
from on_the_fly.infrastructure.translation.engines import TranslationEngine
from on_the_fly.infrastructure.translation.pivot import PIVOT_LANGUAGE, PivotTranslator


class Leg:
    """One hop. Records what it was asked and returns a marked-up string."""

    def __init__(self, produces: str = "") -> None:
        self.seen: list[tuple[str, str, str]] = []
        self._produces = produces

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        self.seen.append((text, source_language, target_language))
        return self._produces or f"{text}|{source_language}->{target_language}"


def bridge(first: Leg, second: Leg, *, source: str = "fr", target: str = "ru") -> PivotTranslator:
    return PivotTranslator(first, second, source_language=source, target_language=target)


# --------------------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------------------


def test_the_output_of_the_first_leg_is_the_input_to_the_second() -> None:
    first, second = Leg("hello"), Leg("привет")
    result = bridge(first, second).translate("bonjour", source_language="fr", target_language="ru")

    assert first.seen == [("bonjour", "fr", "en")]
    assert second.seen == [("hello", "en", "ru")]
    assert result == "привет"


def test_an_empty_first_leg_stops_rather_than_asking_the_second_to_invent_one() -> None:
    """A model handed an empty string returns whatever it makes of nothing. There is no
    sentence to carry, so there is nothing to translate."""
    first, second = Leg(produces="   "), Leg()

    assert bridge(first, second).translate("", source_language="fr", target_language="ru") == ""
    assert second.seen == [], "the second leg was asked to translate nothing"


def test_a_route_refuses_a_pair_it_was_not_built_for() -> None:
    """The port takes the languages on every call, and a route that ignored them would
    translate one pair while claiming another."""
    with pytest.raises(ValueError, match="translates fr->ru"):
        bridge(Leg(), Leg()).translate("x", source_language="ru", target_language="fr")


@pytest.mark.parametrize(("source", "target"), [("fr", "fr"), ("en", "ru"), ("fr", "en")])
def test_a_pair_with_nothing_to_bridge_is_refused_at_construction(source: str, target: str) -> None:
    """One side already being the bridge makes a leg the identity, and a language to itself
    translates nothing. Either way the pair is served directly or not at all."""
    with pytest.raises(ValueError):
        PivotTranslator(Leg(), Leg(), source_language=source, target_language=target)


def test_the_route_says_what_it_is_without_carrying_any_text() -> None:
    rendered = repr(bridge(Leg(), Leg()))

    assert rendered == "PivotTranslator(fr->en->ru)"


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", list(TranslationEngine))
@pytest.mark.parametrize(("source", "target"), [("fr", "ru"), ("ru", "fr")])
def test_the_unpinned_pair_resolves_through_english_on_both_engines(
    engine: TranslationEngine, source: str, target: str
) -> None:
    """Both engines or neither. The bridge exists on ONNX because both its legs do, so
    "does this pair work on a phone" still has one answer."""
    choice = resolve_engine((source, target), engine)

    assert choice.is_pivot
    assert choice.via == PIVOT_LANGUAGE
    assert choice.pair == (source, target)
    assert choice.engine is engine


def test_a_pinned_pair_is_never_bridged() -> None:
    """Two hops where one would do is slower and worse. The direct artefact wins."""
    for artefact in KNOWN_ARTIFACTS.values():
        choice = resolve_engine(artefact.pair)

        assert not choice.is_pivot, f"{artefact.pair} is pinned and was bridged anyway"
        assert choice.name == artefact.name


def test_the_route_is_printed_with_the_bridge_in_it() -> None:
    """The command line prints this before the first translation appears. A bridged pair
    that rendered like a direct one would hide two models behind one name."""
    assert "fr->en->ru" in str(resolve_engine(("fr", "ru")))
    assert "fr->ru" not in str(resolve_engine(("fr", "ru")))


def test_both_models_are_credited() -> None:
    """CC-BY-4.0 asks to be told about the work it covers, and two works were used. An
    attribution naming one of them credits the wrong half."""
    choice = resolve_engine(("fr", "ru"))

    assert choice.attribution.count("OPUS-MT") >= 2
    assert "French-English" in choice.attribution
    assert "English-Russian" in choice.attribution
    assert choice.name == "opus-mt-fr-en + opus-mt-en-ru"


def test_a_licence_is_not_flattened_when_the_legs_agree() -> None:
    """Both legs are CC-BY-4.0 here, so the pair is CC-BY-4.0 rather than the string twice."""
    assert resolve_engine(("fr", "ru")).licence == "CC-BY-4.0"


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_a_pair_that_cannot_be_bridged_keeps_the_message_it_was_owed() -> None:
    """`es->en` has English on one side, so there is nothing to bridge. The reader gets the
    refusal about the pair they asked for, listing what is pinned — not a message about
    whichever leg happened to be missing."""
    with pytest.raises(TranslationArtifactError, match="no pinned translation model for es->en"):
        resolve_engine(("es", "en"))


def test_a_bridgeable_shape_with_a_missing_leg_says_so() -> None:
    """`es->ru` is the shape a bridge fits — neither side is English — and the leg that
    would carry it is not pinned. There the bridge is the part worth explaining."""
    with pytest.raises(TranslationArtifactError, match="cannot be reached through en"):
        resolve_engine(("es", "ru"))


def test_a_language_to_itself_is_refused_rather_than_bridged() -> None:
    with pytest.raises(TranslationArtifactError):
        resolve_engine(("es", "es"))


def test_english_bridges_because_every_pinned_pair_touches_it() -> None:
    """The choice of bridge, asserted rather than assumed. A pinned pair with no English
    side would make this the wrong language to route through."""
    for artefact in KNOWN_ARTIFACTS.values():
        assert PIVOT_LANGUAGE in artefact.pair, f"{artefact.pair} does not touch the bridge"


# --------------------------------------------------------------------------------------
# What the user is told
# --------------------------------------------------------------------------------------


def test_the_command_line_says_a_pair_is_bridged(capsys: pytest.CaptureFixture[str]) -> None:
    """Two models and a third language are a thing to be told, not to infer from the plus
    sign in a model name."""
    describe_translation(resolve_engine(("fr", "ru")))
    printed = capsys.readouterr().out

    assert "fr->en->ru" in printed
    assert "no single pinned model serves this pair" in printed


def test_a_direct_pair_gains_no_extra_line(capsys: pytest.CaptureFixture[str]) -> None:
    describe_translation(resolve_engine(("fr", "en")))
    printed = capsys.readouterr().out

    assert len(printed.splitlines()) == 2, printed
    assert "->" not in printed


def test_both_models_are_credited_where_a_user_can_read_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CC-BY-4.0 attribution that is computed and never displayed is not attribution."""
    choice = resolve_engine(("ru", "fr"))
    describe_translation(choice)

    assert choice.attribution in capsys.readouterr().out
