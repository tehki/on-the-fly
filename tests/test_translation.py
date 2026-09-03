"""Tests for translation (ADR 0009).

Offline and deterministic (handbook 18). The engine and the tokenisers are fakes, so these
run without an 80 MB model — the real model is exercised by hand and its measurements live
in `docs/PERFORMANCE_BUDGET.md`, which is the right place for a number that depends on the
machine that produced it.

The case-restoration tests are the ones that matter most. Uppercase input does not raise
an error; it silently produces a worse translation, which is the failure mode nobody
notices in a language they cannot read.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from on_the_fly.infrastructure.translation import (
    ArtifactIntegrityError,
    ArtifactNotPresentError,
    MarianArtifact,
    OpusMtTranslator,
    TranslationArtifactError,
    TranslationError,
    TranslationModelStore,
    UnsupportedPairError,
    resolve,
    sentence_case,
)
from on_the_fly.infrastructure.translation.artifacts import (
    OPUS_MT_EN_RU,
    OPUS_MT_RU_EN,
    file_digest,
)


class FakeResult:
    def __init__(self, hypotheses: list[list[str]]) -> None:
        self.hypotheses = hypotheses


class FakeEngine:
    """Records what it was asked to translate and returns a fixed hypothesis."""

    def __init__(self, hypothesis: list[str] | None = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[list[str]], dict[str, Any]]] = []
        self._hypothesis = hypothesis if hypothesis is not None else ["перевод"]
        self._fail = fail

    def translate_batch(self, source: Any, /, **options: Any) -> Any:
        self.calls.append((list(source), options))
        if self._fail:
            raise ValueError("engine exploded, and the source text was <secret utterance>")
        return [FakeResult([self._hypothesis])]


class FakeTokeniser:
    """Splits on spaces. Enough to assert what reached the engine."""

    def encode(self, text: str, /, *, out_type: Any = None) -> list[str]:
        return text.split()


class FakeDecoder:
    def decode(self, pieces: Any, /) -> str:
        return " ".join(pieces)


def build(engine: FakeEngine | None = None, **kwargs: Any) -> OpusMtTranslator:
    return OpusMtTranslator(
        engine or FakeEngine(),
        FakeTokeniser(),
        FakeDecoder(),
        source_language=kwargs.pop("source_language", "en"),
        target_language=kwargs.pop("target_language", "ru"),
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# Case restoration. Measured on the real model: uppercase input turns
# "THE MEETING IS AT THREE O'CLOCK ON TUESDAY" into a non-translation, and takes longer
# doing it. These pin the fix.
# --------------------------------------------------------------------------------------


def test_uppercase_recogniser_output_is_recased() -> None:
    assert sentence_case("THE MEETING IS AT THREE O'CLOCK ON TUESDAY") == (
        "The meeting is at three o'clock on tuesday"
    )


def test_the_real_recogniser_sample_is_recased() -> None:
    """The exact transcript ADR 0008 recorded."""
    recognised = "AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP"

    assert sentence_case(recognised).startswith("After early nightfall")


def test_ordinary_prose_keeps_its_capitals() -> None:
    """Re-casing must not destroy real casing it did not need to fix."""
    text = "I spoke to Maria in Berlin on Tuesday."

    assert sentence_case(text) == text


def test_mixed_case_is_left_alone() -> None:
    assert sentence_case("The NHS said no") == "The NHS said no"


def test_surrounding_whitespace_is_removed() -> None:
    assert sentence_case("  HELLO THERE  ") == "Hello there"


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_input_stays_empty(text: str) -> None:
    assert sentence_case(text) == ""


def test_digits_and_punctuation_do_not_defeat_the_uppercase_check() -> None:
    """`isupper()` is False for digits, so a naive all-upper test would miss this."""
    assert sentence_case("I NEED 45 EUROS BY 3 O'CLOCK") == "I need 45 euros by 3 o'clock"


# --------------------------------------------------------------------------------------
# The translator itself
# --------------------------------------------------------------------------------------


def test_a_translation_is_returned() -> None:
    translator = build(FakeEngine(["Доброе", "утро"]))

    assert translator.translate("Good morning", source_language="en", target_language="ru") == (
        "Доброе утро"
    )


def test_the_engine_receives_recased_text() -> None:
    """The whole point: what reaches the model is not what the recogniser emitted."""
    engine = FakeEngine()
    translator = build(engine)

    translator.translate("GOOD MORNING", source_language="en", target_language="ru")

    ((batch, _options),) = engine.calls
    assert batch == [["Good", "morning"]]


def test_the_publishers_beam_size_is_used_by_default() -> None:
    """6 comes from the artefact's own decoder.yml, not from taste."""
    engine = FakeEngine()

    build(engine).translate("hello", source_language="en", target_language="ru")

    assert engine.calls[0][1]["beam_size"] == 6


def test_a_pair_this_model_does_not_serve_is_refused() -> None:
    """Directional weights. Attempting the reverse would produce confident nonsense."""
    translator = build()

    with pytest.raises(UnsupportedPairError, match="en->ru"):
        translator.translate("привет", source_language="ru", target_language="en")


def test_a_refused_pair_never_reaches_the_engine() -> None:
    engine = FakeEngine()
    translator = build(engine)

    with pytest.raises(UnsupportedPairError):
        translator.translate("hello", source_language="en", target_language="de")

    assert engine.calls == []


def test_language_codes_are_case_insensitive() -> None:
    translator = build()

    assert translator.translate("hi", source_language="EN", target_language="RU")


def test_empty_text_translates_to_nothing_without_calling_the_engine() -> None:
    """An empty caption clears the screen for nothing."""
    engine = FakeEngine()

    assert build(engine).translate("   ", source_language="en", target_language="ru") == ""
    assert engine.calls == []


def test_an_engine_failure_becomes_a_typed_error_without_echoing_the_text() -> None:
    """The engine's exception carried the utterance. It must not travel with the error."""
    translator = build(FakeEngine(fail=True))

    with pytest.raises(TranslationError) as caught:
        translator.translate("something private", source_language="en", target_language="ru")

    assert "secret utterance" not in str(caught.value)
    assert "something private" not in str(caught.value)


def test_no_hypothesis_is_an_error_not_an_empty_string() -> None:
    engine = FakeEngine()
    engine.translate_batch = lambda source, /, **options: [FakeResult([])]  # type: ignore[method-assign]

    with pytest.raises(TranslationError, match="no hypothesis"):
        build(engine).translate("hello", source_language="en", target_language="ru")


def test_repr_carries_no_project_content() -> None:
    """This object handles EPHEMERAL text and its repr reaches logs (Article 14)."""
    rendered = repr(build())

    assert "en->ru" in rendered
    assert "beam_size" in rendered


def test_nothing_is_cached_between_calls() -> None:
    """ADR 0009: a translation cache retains project content past its window."""
    engine = FakeEngine()
    translator = build(engine)

    translator.translate("hello", source_language="en", target_language="ru")
    translator.translate("hello", source_language="en", target_language="ru")

    assert len(engine.calls) == 2


# --------------------------------------------------------------------------------------
# The pinned artefact
# --------------------------------------------------------------------------------------


def test_the_pinned_english_russian_artefact_is_declared_correctly() -> None:
    assert OPUS_MT_EN_RU.pair == ("en", "ru")
    assert OPUS_MT_EN_RU.licence == "CC-BY-4.0"
    assert len(OPUS_MT_EN_RU.sha256) == 64
    assert OPUS_MT_EN_RU.url.startswith("https://")


def test_the_licence_that_travels_with_the_artefact_is_the_one_recorded() -> None:
    """The Hugging Face mirror says apache-2.0; the archive's own LICENSE says CC-BY-4.0.

    Recorded as a test because the disagreement is the kind of detail that gets 'tidied'
    later by someone reading the model page rather than the artefact.
    """
    assert OPUS_MT_EN_RU.licence == "CC-BY-4.0"
    assert "CC-BY-4.0" in OPUS_MT_EN_RU.attribution
    assert "Helsinki-NLP" in OPUS_MT_EN_RU.attribution


def test_an_artefact_must_be_fetched_over_https() -> None:
    with pytest.raises(ValueError, match="https"):
        MarianArtifact(
            name="x",
            url="http://example.invalid/model.zip",
            sha256="a" * 64,
            source_language="en",
            target_language="ru",
            licence="CC-BY-4.0",
            attribution="x",
        )


def test_an_artefact_must_carry_a_real_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        MarianArtifact(
            name="x",
            url="https://example.invalid/model.zip",
            sha256="tooshort",
            source_language="en",
            target_language="ru",
            licence="CC-BY-4.0",
            attribution="x",
        )


def test_the_pinned_russian_english_artefact_is_declared_correctly() -> None:
    assert OPUS_MT_RU_EN.pair == ("ru", "en")
    assert OPUS_MT_RU_EN.licence == "CC-BY-4.0"
    assert len(OPUS_MT_RU_EN.sha256) == 64


def test_the_two_directions_are_separate_artefacts() -> None:
    """OPUS-MT models are directional; one pin cannot serve both ways."""
    assert OPUS_MT_EN_RU.sha256 != OPUS_MT_RU_EN.sha256
    assert OPUS_MT_EN_RU.url != OPUS_MT_RU_EN.url
    assert OPUS_MT_EN_RU.pair == tuple(reversed(OPUS_MT_RU_EN.pair))


def test_the_later_russian_english_release_is_the_pinned_one() -> None:
    """Two releases exist for this pair. ADR 0011 takes the one that scores better.

    Pinned by exact URL rather than by name, because "the ru-en model" names two things.
    """
    assert "opus-2020-02-26" in OPUS_MT_RU_EN.url


def test_both_directions_carry_their_attribution() -> None:
    """CC-BY-4.0 obliges attribution for each artefact actually used, not once overall."""
    for artefact in (OPUS_MT_EN_RU, OPUS_MT_RU_EN):
        assert "CC-BY-4.0" in artefact.attribution
        assert "Helsinki-NLP" in artefact.attribution


def test_resolving_the_reverse_pair_finds_the_reverse_artefact() -> None:
    assert resolve(("ru", "en")) is OPUS_MT_RU_EN
    assert resolve(("en", "ru")) is OPUS_MT_EN_RU


def test_resolving_a_pair_with_no_pinned_model_is_refused() -> None:
    with pytest.raises(TranslationArtifactError, match="no pinned translation model"):
        resolve(("en", "de"))


def test_resolving_the_pinned_pair_finds_it() -> None:
    assert resolve(("en", "ru")) is OPUS_MT_EN_RU


# --------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------


def fake_artifact(tmp_path: Path, digest: str) -> MarianArtifact:
    return MarianArtifact(
        name="fake",
        url="https://example.invalid/model.zip",
        sha256=digest,
        source_language="en",
        target_language="ru",
        licence="CC-BY-4.0",
        attribution="fake",
        members=("decoder.yml", "source.spm"),
    )


def write_archive(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("decoder.yml", "beam-size: 6\n")
        bundle.writestr("source.spm", "not a real sentencepiece model")
        bundle.writestr("train.log", "noise that must not be extracted")
    return file_digest(path)


def test_a_missing_artefact_is_refused_when_downloading_is_off(tmp_path: Path) -> None:
    """Downloading is opt-in, exactly as it is for recognition models."""
    artefact = fake_artifact(tmp_path, "a" * 64)
    store = TranslationModelStore(tmp_path, allow_download=False)

    with pytest.raises(ArtifactNotPresentError, match="downloading is not enabled"):
        store.ensure(artefact)


def test_a_digest_mismatch_is_refused_and_the_file_is_left_alone(tmp_path: Path) -> None:
    artefact = fake_artifact(tmp_path, "b" * 64)
    store = TranslationModelStore(tmp_path, allow_download=True)
    archive = store.archive_path(artefact)
    write_archive(archive)

    with pytest.raises(ArtifactIntegrityError, match="failed verification"):
        store.ensure(artefact)

    assert archive.is_file(), "the evidence of a possible supply-chain event was destroyed"


def test_a_matching_digest_verifies(tmp_path: Path) -> None:
    staging = tmp_path / "staging.zip"
    digest = write_archive(staging)
    artefact = fake_artifact(tmp_path, digest)
    store = TranslationModelStore(tmp_path, allow_download=False)
    archive = store.archive_path(artefact)
    archive.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(archive)

    store.verify(artefact, archive)


def test_only_pinned_members_are_extracted(tmp_path: Path) -> None:
    """The archive carries training logs and the publisher's shell scripts. Neither is ours."""
    staging = tmp_path / "staging.zip"
    digest = write_archive(staging)
    artefact = fake_artifact(tmp_path, digest)
    store = TranslationModelStore(tmp_path, allow_download=False)
    archive = store.archive_path(artefact)
    archive.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(archive)

    source = store.source_dir(artefact)
    store._extract(artefact, archive, source)

    assert (source / "decoder.yml").is_file()
    assert (source / "source.spm").is_file()
    assert not (source / "train.log").exists()


def test_the_cache_is_keyed_by_digest(tmp_path: Path) -> None:
    """A changed pin must never collide with what an older pin left behind."""
    one = fake_artifact(tmp_path, "c" * 64)
    two = fake_artifact(tmp_path, "d" * 64)
    store = TranslationModelStore(tmp_path)

    assert store.archive_path(one) != store.archive_path(two)
    assert store.converted_dir(one) != store.converted_dir(two)


def test_store_repr_states_whether_downloading_is_enabled(tmp_path: Path) -> None:
    assert "allow_download=False" in repr(TranslationModelStore(tmp_path))
