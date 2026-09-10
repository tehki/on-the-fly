"""Tests for what this project offers, as opposed to what it names.

`domain/languages.py` says all seven languages are served at the streaming tier, and that is
true of the published models. Only two of them have a model *pinned* here, and only one pair
has a translation model in either direction. The gap between those two facts is a window that
offered forty-nine language pairs and could serve two — with the refusal arriving after the
user pressed Listen, in the shape of a `KeyError` repr.

So these tests hold the offer to the pins. They are written against the registries rather
than against a hardcoded expectation wherever they can be, so that pinning a third language
makes them describe the new state instead of failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from on_the_fly.app.catalogue import (
    can_recognise,
    recognisable_languages,
    servable_pairs,
    streaming_pin_name,
    translation_targets,
)
from on_the_fly.app.cli import main
from on_the_fly.domain.languages import SUPPORTED, Language, RecognitionTier
from on_the_fly.infrastructure.asr import DEFAULT_MODEL
from on_the_fly.infrastructure.asr.models import KNOWN_MODELS
from on_the_fly.infrastructure.model_store import ModelPin
from on_the_fly.infrastructure.translation import KNOWN_ARTIFACTS, KNOWN_ONNX_MODELS
from on_the_fly.infrastructure.translation.engines import TranslationEngine
from on_the_fly.ui.app import streaming_languages, translation_options
from on_the_fly.ui.caption import NO_TRANSLATION

# --------------------------------------------------------------------------------------
# Recognition: the tier is not the question. The pin is.
# --------------------------------------------------------------------------------------


def test_only_languages_with_a_pinned_model_can_be_recognised() -> None:
    """Every entry answers to a pin, and every pin has an entry — derived, not listed."""
    offered = {lang.code for lang in recognisable_languages()}
    pinned = {code for code in SUPPORTED if streaming_pin_name(code) in KNOWN_MODELS}

    assert offered == pinned


def test_the_unadopted_languages_are_not_offered() -> None:
    """Spanish, Italian, Portuguese and German are named because a published model exists
    (ADR 0007), not because one has been adopted, licence-checked or tested.

    French was the fifth until ADR 0031 pinned one. The four that remain are not waiting on
    effort: the five-language family that covers them points at an empty LICENSE file, and
    the one Apache-2.0 Spanish model found emits phonemes rather than words.
    """
    offered = {lang.code for lang in recognisable_languages()}

    assert offered == {"en", "ru", "fr"}
    assert not offered & {"es", "it", "pt", "de"}


def test_a_pin_and_a_tier_without_a_layout_is_not_recognisable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third leg, and the one a contributor is likeliest to miss.

    Which files in a pinned directory are the encoder, decoder and joiner lives in
    `STREAMING_LAYOUTS`, a separate dict from the pins it keys. Adding a pin and moving the
    language to the streaming tier — the two visible steps of adopting a language — left
    this module reporting the language as recognisable, an interface offering it, and the
    recogniser raising a bare `KeyError` at the moment it was chosen.
    """
    monkeypatch.setitem(
        KNOWN_MODELS,
        "streaming-de",
        ModelPin(
            name="streaming-de",
            repo_id="example/de",
            revision="0" * 40,
            licence="Apache-2.0",
            digests={"encoder.onnx": "a" * 64},
        ),
    )
    monkeypatch.setitem(SUPPORTED, "de", Language("de", "German", RecognitionTier.STREAMING))

    assert not can_recognise("de")
    assert "de" not in {lang.code for lang in recognisable_languages()}


def test_an_unknown_language_is_not_recognisable() -> None:
    assert not can_recognise("tg"), "Tajik was removed by ADR 0010"
    assert not can_recognise("klingon")


def test_recognisable_languages_are_ordered_by_name() -> None:
    """The order they are read in, not the order their codes sort in."""
    names = [lang.name for lang in recognisable_languages()]

    assert names == sorted(names)


# --------------------------------------------------------------------------------------
# Translation: which targets exist depends on the source, because the pairs do.
# --------------------------------------------------------------------------------------


def test_translation_targets_come_from_the_pinned_pairs() -> None:
    """Ordered by language name, which is why French precedes German precedes Russian.

    Four languages translate and each reaches all three others. Only half of those pairs are
    pinned: the rest are bridged through English (ADR 0037, ADR 0038), which the catalogue
    does not need to know — it asks the engine resolver the same question it always did.
    """
    assert [lang.code for lang in translation_targets("en")] == ["fr", "de", "ru"]
    assert [lang.code for lang in translation_targets("ru")] == ["en", "fr", "de"]
    assert [lang.code for lang in translation_targets("fr")] == ["en", "de", "ru"]
    assert [lang.code for lang in translation_targets("de")] == ["en", "fr", "ru"]
    assert [lang.code for lang in translation_targets("it")] == ["en", "fr", "de", "ru"]


def test_a_source_with_no_pinned_pair_offers_no_targets() -> None:
    """Spanish is recognised at the batch tier and translated by nothing. German was here
    until ADR 0038, and the difference between them is a pinned artefact, not a tier."""
    assert translation_targets("es") == ()


def test_italian_can_be_translated_from_and_not_into() -> None:
    """The asymmetry is the decision, not an oversight (ADR 0039).

    `en-it` publishes a sentencepiece archive that would convert here; what it has no usable
    ONNX export of is the other engine, so pinning it would serve the pair on the desktop and
    never on a phone. German is the mirror image — a target that cannot be a source — and
    between them they are why the two questions are asked separately.
    """
    assert "en" in {lang.code for lang in translation_targets("it")}
    for source in ("en", "fr", "de", "ru"):
        assert "it" not in {lang.code for lang in translation_targets(source)}, (
            f"{source}->it is offered and nothing pins it"
        )


def test_a_language_is_never_a_translation_target_for_itself() -> None:
    """Translating English into English is not a pair; it is the absence of one."""
    for code in SUPPORTED:
        assert code not in {lang.code for lang in translation_targets(code)}


def test_the_onnx_engine_is_asked_about_its_own_artefacts() -> None:
    """Resolving is per engine (ADR 0018): a pair served by CTranslate2 and not by ONNX must
    not be offered to a caller who asked for the engine that runs on a phone."""
    onnx = {
        (source, lang.code)
        for source in SUPPORTED
        for lang in translation_targets(source, engine=TranslationEngine.ONNX)
    }
    ctranslate2 = {
        (source, lang.code)
        for source in SUPPORTED
        for lang in translation_targets(source, engine=TranslationEngine.CTRANSLATE2)
    }

    # The point of the test, unchanged: the two engines offer the same thing, so "does this
    # work on a phone" has one answer per pair rather than one per engine.
    assert onnx == ctranslate2

    # What each offers is no longer identical to what each pins. Since ADR 0037 a pair with
    # no artefact can be bridged through English, and that route exists on both engines
    # because both its legs do. Every pinned pair is still offered directly.
    assert {model.pair for model in KNOWN_ONNX_MODELS.values()} <= onnx
    assert {artefact.pair for artefact in KNOWN_ARTIFACTS.values()} <= ctranslate2
    assert onnx - {model.pair for model in KNOWN_ONNX_MODELS.values()} == {
        ("fr", "ru"),
        ("ru", "fr"),
        ("de", "ru"),
        ("ru", "de"),
        ("de", "fr"),
        ("fr", "de"),
        ("it", "fr"),
        ("it", "de"),
        ("it", "ru"),
    }, "everything that does not touch English, and nothing that does"


def test_servable_pairs_needs_both_a_recogniser_and_a_translator() -> None:
    """A pair is only end-to-end servable when the source can be heard and the pair written."""
    pairs = {(source.code, target.code) for source, target in servable_pairs()}

    assert pairs == {
        ("en", "ru"),
        ("ru", "en"),
        ("en", "fr"),
        ("fr", "en"),
        # Bridged through English rather than pinned (ADR 0037).
        ("fr", "ru"),
        ("ru", "fr"),
        # German is a target and not a source: it translates (ADR 0038) but does not stream,
        # so somebody speaking English can be read in German while the reverse needs a file
        # and the batch tier.
        ("en", "de"),
        ("fr", "de"),
        ("ru", "de"),
    }
    assert len(pairs) == 9, "three streaming sources, three targets each"


# --------------------------------------------------------------------------------------
# And the window offers exactly that. This is the defect these tests exist for.
# --------------------------------------------------------------------------------------


def test_the_source_picker_offers_only_pinned_languages() -> None:
    assert streaming_languages() == [(lang.code, lang.name) for lang in recognisable_languages()]


def test_every_pair_the_pickers_can_produce_can_be_served() -> None:
    """The claim in one assertion: no combination of the two pickers reaches a pair that
    would fail once the microphone is open."""
    servable = {(source.code, target.code) for source, target in servable_pairs()}

    for source_code, _ in streaming_languages():
        for target_code, _ in translation_options(source_code):
            captions_only = target_code == NO_TRANSLATION
            assert captions_only or (source_code, target_code) in servable


def test_captions_without_translation_is_offered_first() -> None:
    """And carries no language code of its own.

    The source's code was the obvious choice — `ViewState` encodes "not translating" as
    target == source — and it was wrong: `ru` means *into Russian* under an English source
    and *not at all* under a Russian one, so switching source silently stopped translating.
    """
    options = translation_options("en")

    assert options[0] == (NO_TRANSLATION, "no translation")
    assert options[1:] == [("fr", "French"), ("de", "German"), ("ru", "Russian")]
    assert NO_TRANSLATION not in SUPPORTED, "the row must not collide with a language"


def test_no_option_row_is_ever_ambiguous() -> None:
    """The property the bug violated: within one source, no two rows share a code."""
    for source_code, _ in streaming_languages():
        codes = [code for code, _ in translation_options(source_code)]
        assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------------------
# `languages`: what this build serves, printed rather than looked up in a README
#
# Everything it prints comes from the same resolvers a run will use moments later, which is
# the property that keeps it from going stale the way the window's pickers once did
# (ADR 0034).
# ---------------------------------------------------------------------------------------


def spoken(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert main(["languages", *argv]) == 0
    return capsys.readouterr().out


def test_it_lists_what_is_recognised_live_and_what_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = spoken(capsys)

    for language in recognisable_languages():
        assert f"{language.name} ({language.code})" in output
    assert "from a file only" in output


def test_every_language_appears_exactly_once_in_the_translation_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A language missing from it is one a user would conclude does not work."""
    output = spoken(capsys)

    for code in SUPPORTED:
        assert output.count(f"\n  {code} -> ") == 1, f"{code} is listed {output.count(code)} times"


def test_a_language_with_no_pinned_pair_says_so_rather_than_being_omitted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "es -> nothing is pinned to translate it" in spoken(capsys)


def test_a_bridged_pair_is_marked_and_the_mark_is_explained(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two models rather than one is a thing to be told, not to infer from a footnote
    nobody explained (ADR 0037)."""
    output = spoken(capsys)

    assert "ru*" in output or "fr*" in output
    assert "reached through English" in output


def test_the_count_it_prints_is_the_one_the_catalogue_computes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = spoken(capsys)

    assert f"{len(servable_pairs())} pair(s) work end to end" in output


def test_it_answers_for_the_engine_it_was_asked_about(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "engine        onnx" in spoken(capsys, "--translation-engine", "onnx")


def test_it_opens_nothing_and_downloads_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one command a user runs to find out what is possible must not need a model, a
    microphone or a network to answer."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("languages reached for a model")

    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", refuse)

    assert main(["languages"]) == 0


# ---------------------------------------------------------------------------------------
# `fetch`: get a pair ready, then stop
#
# Every other command discovers a missing model in the middle of doing something. On this
# machine a translation model takes 9 to 15 minutes to arrive and 13 seconds to convert,
# which is a bad thing to find out once somebody is already talking.
# ---------------------------------------------------------------------------------------


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Records what `fetch` asked for without fetching anything."""
    seen: dict[str, object] = {}

    def fake_ensure(self: object, pin: object) -> Path:
        seen["pin"] = getattr(pin, "name", pin)
        seen["allow_download"] = getattr(self, "allow_download", None)
        return Path("/nowhere")

    def fake_open(choice: object, cache_dir: object, **kwargs: object) -> object:
        seen["choice"] = str(choice)
        seen["translation_allow_download"] = kwargs.get("allow_download")
        return object()

    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", fake_ensure)
    monkeypatch.setattr("on_the_fly.app.cli.open_translator", fake_open)
    monkeypatch.setattr(
        "on_the_fly.app.cli.SherpaStreamingRecognizer", lambda *a, **k: _QuietRecognizer()
    )
    monkeypatch.setattr("on_the_fly.app.cli.FasterWhisperRecognizer", lambda *a, **k: object())
    return seen


class _QuietRecognizer:
    def warm_up(self) -> None: ...

    def validate_format(self, audio_format: object) -> None: ...


def test_fetching_a_streaming_pair_prepares_both_models(
    fetched: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fetch", "--language", "fr", "--translate-to", "ru"]) == 0

    assert fetched["pin"] == "streaming-fr"
    assert "fr->en->ru" in str(fetched["choice"]), "the bridged route was not prepared"
    assert "nothing left to download" in capsys.readouterr().out


def test_fetching_asks_for_the_download_that_every_other_command_refuses(
    fetched: dict[str, object],
) -> None:
    """`--allow-download` defaults to false everywhere else, which is the control this
    command exists to spend deliberately."""
    assert main(["fetch", "--language", "fr", "--translate-to", "ru"]) == 0

    assert fetched["allow_download"] is True
    assert fetched["translation_allow_download"] is True


def test_a_batch_language_fetches_the_batch_model(fetched: dict[str, object]) -> None:
    """Italian does not stream, so there is no streaming pin to fetch for it (ADR 0039)."""
    assert main(["fetch", "--language", "it", "--translate-to", "en"]) == 0

    assert fetched["pin"] == DEFAULT_MODEL.name


def test_the_batch_branch_asks_for_the_download_too(fetched: dict[str, object]) -> None:
    """The streaming branch was asserted and this one was not, so a mutation setting it to
    false survived: `fetch` for a batch language would have refused to download the very
    thing it exists to fetch, silently."""
    assert main(["fetch", "--language", "it", "--translate-to", "en"]) == 0

    assert fetched["allow_download"] is True
    assert fetched["translation_allow_download"] is True


def test_a_batch_language_can_be_asked_for_a_different_size(fetched: dict[str, object]) -> None:
    assert main(["fetch", "--language", "it", "--model", "small"]) == 0

    assert fetched["pin"] == "small"


def test_captions_only_needs_no_translation_model(fetched: dict[str, object]) -> None:
    assert main(["fetch", "--language", "fr"]) == 0

    assert "choice" not in fetched


def test_a_pair_nothing_serves_is_refused_before_anything_is_downloaded(
    fetched: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fetch", "--language", "es", "--translate-to", "en"]) == 1

    assert "no pinned translation model" in capsys.readouterr().err
    assert fetched == {}, "something was fetched for a pair that cannot be served"


def test_a_language_into_itself_is_refused(
    fetched: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fetch", "--language", "fr", "--translate-to", "fr"]) == 1

    assert "nothing to translate" in capsys.readouterr().err


def test_fetching_without_a_language_is_refused_by_the_parser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """There is no sensible default: fetching "the models" without saying for what would
    either fetch everything or guess."""
    with pytest.raises(SystemExit):
        main(["fetch", "--translate-to", "en"])

    assert "--language" in capsys.readouterr().err


def test_a_language_that_streams_is_labelled_live_and_one_that_does_not_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The label is the difference between "speak and read it" and "record it first", which
    is most of what a user needs from this command."""
    output = spoken(capsys)

    for line in output.splitlines():
        if line.startswith("  en -> "):
            assert "(live)" in line
        if line.startswith("  it -> "):
            assert "(from a file)" in line, "Italian does not stream (ADR 0039)"
