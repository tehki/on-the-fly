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
    open_translator,
    resolve,
    resolve_engine,
    sentence_case,
)
from on_the_fly.infrastructure.translation.artifacts import (
    CONVERSION_MARKER,
    KNOWN_ARTIFACTS,
    OPUS_MT_DE_EN,
    OPUS_MT_EN_DE,
    OPUS_MT_EN_FR,
    OPUS_MT_EN_RU,
    OPUS_MT_FR_EN,
    OPUS_MT_IT_EN,
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


def test_decoding_is_greedy_by_default() -> None:
    """Beam 1, not the publisher's 6. Measured, not assumed: on their own test set and
    human references, chrF2 was 66.62 greedy against 66.56 at beam 6, at 2.3x the speed.
    A default that costs nothing measurable and buys the latency budget."""
    engine = FakeEngine()

    build(engine).translate("hello", source_language="en", target_language="ru")

    assert engine.calls[0][1]["beam_size"] == 1


def test_the_loader_and_the_constructor_agree_on_every_default() -> None:
    """The regression this exists for.

    `OpusMtTranslator.__init__` defaulted to greedy while `load()` defaulted to beam 6 and
    passed it explicitly, so every translation the application performed used beam 6 while
    this file — building the class directly — asserted greedy. The unit test passed and the
    product did the other thing for two merges.

    Comparing the signatures catches that whatever the values are.
    """
    import inspect

    from on_the_fly.infrastructure.translation import opus_mt

    constructor = inspect.signature(opus_mt.OpusMtTranslator.__init__).parameters
    loader = inspect.signature(opus_mt.load).parameters

    shared = set(constructor) & set(loader) - {"self"}
    assert "beam_size" in shared, "the loader must expose the decoding setting"
    for name in shared:
        assert constructor[name].default == loader[name].default, (
            f"{name} defaults differ: constructor={constructor[name].default}, "
            f"load()={loader[name].default}. A caller using load() would silently get "
            f"different behaviour from one building the class directly."
        )


def test_thread_use_is_bounded_by_default() -> None:
    """Measured: unbounded threads are 6.9x slower under load and miss the budget."""
    from on_the_fly.infrastructure.translation import opus_mt

    assert opus_mt.DEFAULT_INTRA_THREADS == 1


def test_the_publishers_beam_size_is_still_reachable() -> None:
    """The trade stays available to a caller who wants the publisher's setting back."""
    engine = FakeEngine()

    build(engine, beam_size=6).translate("hello", source_language="en", target_language="ru")

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


def test_every_direction_carries_its_attribution() -> None:
    """CC-BY-4.0 obliges attribution for each artefact actually used, not once overall."""
    for artefact in KNOWN_ARTIFACTS.values():
        assert "CC-BY-4.0" in artefact.attribution
        assert "Helsinki-NLP" in artefact.attribution


# --------------------------------------------------------------------------------------
# French (ADR 0032). The pair that showed two releases of one direction need not even be
# the same kind of artefact.
# --------------------------------------------------------------------------------------


def test_the_pinned_french_artefacts_are_declared_correctly() -> None:
    assert OPUS_MT_EN_FR.pair == ("en", "fr")
    assert OPUS_MT_FR_EN.pair == ("fr", "en")
    for artefact in (OPUS_MT_EN_FR, OPUS_MT_FR_EN):
        assert artefact.licence == "CC-BY-4.0"
        assert len(artefact.sha256) == 64
        assert artefact.url.startswith("https://")
    assert OPUS_MT_EN_FR.sha256 != OPUS_MT_FR_EN.sha256


def test_the_sentencepiece_french_releases_are_the_pinned_ones() -> None:
    """`fr-en/opus-2019-12-05` scores marginally better and is deliberately not pinned.

    It is a BPE model — its manifest says `normalization + tokenization + BPE`, and it ships
    `source.bpe` where `opus_mt.py` needs `source.spm`. Loading it means admitting a BPE
    implementation under Article 12 to buy 0.11 chrF2. Pinned by exact URL, so the rejection
    is visible in the pin rather than only in an ADR.
    """
    assert "opus-2020-02-26" in OPUS_MT_EN_FR.url
    assert "opus-2020-02-26" in OPUS_MT_FR_EN.url
    assert "2019" not in OPUS_MT_FR_EN.url


def test_the_pinned_german_artefacts_are_declared_correctly() -> None:
    assert OPUS_MT_DE_EN.pair == ("de", "en")
    assert OPUS_MT_EN_DE.pair == ("en", "de")
    for artefact in (OPUS_MT_DE_EN, OPUS_MT_EN_DE):
        assert artefact.licence == "CC-BY-4.0"
        assert len(artefact.sha256) == 64
        assert artefact.url.startswith("https://")
    assert OPUS_MT_DE_EN.sha256 != OPUS_MT_EN_DE.sha256


def test_the_sentencepiece_german_releases_are_the_pinned_ones() -> None:
    """German publishes **three** releases per direction and only the 2020 one is usable.

    The publisher's own `.yml` records 2019-12-04 and 2019-12-18 as `normalization +
    tokenization + BPE`, which is the release ADR 0032 refused for French. Two thirds of the
    published artefacts for this pair are the wrong kind, and nothing in a score table says
    so — which is why the pin is an exact URL and this test reads it.
    """
    assert "opus-2020-02-26" in OPUS_MT_DE_EN.url
    assert "opus-2020-02-26" in OPUS_MT_EN_DE.url
    assert "2019" not in OPUS_MT_DE_EN.url
    assert "2019" not in OPUS_MT_EN_DE.url


def test_german_translates_without_being_recognisable_here() -> None:
    """The two questions are independent, and this pair is the proof (ADR 0038).

    German has no streaming pin and is transcribed an utterance at a time; it still
    translates in both directions. A target needs a translation model and nothing else.
    """
    assert resolve(("en", "de")) is OPUS_MT_EN_DE
    assert resolve(("de", "en")) is OPUS_MT_DE_EN


def test_the_pinned_italian_artefact_is_declared_correctly() -> None:
    assert OPUS_MT_IT_EN.pair == ("it", "en")
    assert OPUS_MT_IT_EN.licence == "CC-BY-4.0"
    assert len(OPUS_MT_IT_EN.sha256) == 64
    assert OPUS_MT_IT_EN.url.startswith("https://")


def test_the_sentencepiece_italian_release_is_the_pinned_one() -> None:
    """`it-en` publishes two releases and the earlier one is BPE — the third pair where that
    is true, after French (ADR 0032) and German (ADR 0038)."""
    assert "opus-2019-12-18" in OPUS_MT_IT_EN.url
    assert "2019-12-05" not in OPUS_MT_IT_EN.url


def test_italian_is_pinned_in_one_direction_only() -> None:
    """Deliberate (ADR 0039): the ONNX export of `en-it` declares no licence and no base
    model, so pinning the archive would serve that pair on one engine. Asserted here so the
    missing direction reads as a decision rather than as something nobody got to."""
    assert resolve(("it", "en")) is OPUS_MT_IT_EN
    with pytest.raises(TranslationArtifactError, match="no pinned translation model"):
        resolve(("en", "it"))


def test_every_pinned_artefact_expects_sentencepiece() -> None:
    """A pin whose members named `.bpe` files would extract and then fail to tokenise."""
    for artefact in KNOWN_ARTIFACTS.values():
        assert "source.spm" in artefact.members
        assert "target.spm" in artefact.members
        assert not any(member.endswith(".bpe") for member in artefact.members)


def test_resolving_the_reverse_pair_finds_the_reverse_artefact() -> None:
    assert resolve(("ru", "en")) is OPUS_MT_RU_EN
    assert resolve(("en", "ru")) is OPUS_MT_EN_RU


def test_resolving_a_pair_with_no_pinned_model_is_refused() -> None:
    """Spanish, which this project recognises at the batch tier and translates not at all.
    German was the example here until ADR 0038 pinned it."""
    with pytest.raises(TranslationArtifactError, match="no pinned translation model"):
        resolve(("en", "es"))


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


# --------------------------------------------------------------------------------------
# A cache that is only half there
#
# `ensure` short-circuits when the converted model *and* the extracted sources are both
# present. Mutation testing found that `and` could be `or` without a test noticing, and the
# same for the `is_dir() and all(members)` behind it. Either flip turns a half-finished
# cache into a cache hit.
#
# This is not hypothetical for these artefacts. They arrive as a 285 MB zip over a slow
# link; an extraction interrupted part way, or a conversion that did not finish, leaves
# exactly this state. Reported as ready, it becomes a translator failing to load with an
# error about a missing file rather than a store that notices and redoes the work.
# --------------------------------------------------------------------------------------


def prepared_store(tmp_path: Path) -> tuple[TranslationModelStore, MarianArtifact, Path]:
    """A store with a verified archive in place, ready for `ensure` to extract."""
    staging = tmp_path / "staging.zip"
    digest = write_archive(staging)
    artefact = fake_artifact(tmp_path, digest)
    store = TranslationModelStore(tmp_path, allow_download=False)
    archive = store.archive_path(artefact)
    archive.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(archive)
    return store, artefact, archive


def test_an_extracted_source_alone_is_not_a_finished_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction finished, conversion did not. The converted model is what gets loaded."""
    store, artefact, _ = prepared_store(tmp_path)
    store._extract(artefact, store.archive_path(artefact), store.source_dir(artefact))
    assert not (store.converted_dir(artefact) / "model.bin").exists()

    converted: list[Path] = []
    monkeypatch.setattr(
        TranslationModelStore,
        "_convert",
        lambda self, source, target: converted.append(target),
    )
    store.ensure(artefact)

    assert converted, "a missing converted model was reported as a cache hit"


def test_a_converted_model_alone_is_not_a_finished_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conversion finished, the sources are gone. They are not spare: the sentencepiece
    tokenisers live there, and the conversion does not produce them."""
    store, artefact, _ = prepared_store(tmp_path)
    converted_dir = store.converted_dir(artefact)
    converted_dir.mkdir(parents=True, exist_ok=True)
    (converted_dir / "model.bin").write_bytes(b"converted")

    monkeypatch.setattr(TranslationModelStore, "_convert", lambda self, source, target: None)
    _, source = store.ensure(artefact)

    for member in artefact.members:
        assert (source / member).is_file(), f"{member} was never extracted"


def test_a_source_directory_missing_one_member_is_not_extracted(tmp_path: Path) -> None:
    """`is_dir() and all(members)`. A directory that exists is not a directory that is
    complete, and an interrupted extraction leaves the first case looking like the second.
    """
    store, artefact, _ = prepared_store(tmp_path)
    source = store.source_dir(artefact)
    store._extract(artefact, store.archive_path(artefact), source)
    assert store._is_extracted(artefact, source)

    (source / artefact.members[-1]).unlink()

    assert not store._is_extracted(artefact, source), "a missing member passed as extracted"


def test_an_empty_source_directory_is_not_extracted(tmp_path: Path) -> None:
    """The state an extraction that failed on its first member leaves behind."""
    store, artefact, _ = prepared_store(tmp_path)
    store.source_dir(artefact).mkdir(parents=True, exist_ok=True)

    assert not store._is_extracted(artefact, store.source_dir(artefact))


def test_a_complete_cache_is_used_without_touching_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule: when both are there, nothing is redone.

    Asserted by deleting the archive first — a store that re-extracts would have to raise.
    """
    store, artefact, archive = prepared_store(tmp_path)
    store._extract(artefact, archive, store.source_dir(artefact))
    converted_dir = finished_conversion(store, artefact)
    archive.unlink()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a complete cache was rebuilt")

    monkeypatch.setattr(TranslationModelStore, "_extract", refuse)
    monkeypatch.setattr(TranslationModelStore, "_convert", refuse)

    assert store.ensure(artefact) == (converted_dir, store.source_dir(artefact))


def finished_conversion(store: TranslationModelStore, artefact: MarianArtifact) -> Path:
    """What `_convert` leaves behind when it gets to the end."""
    converted = store.converted_dir(artefact)
    converted.mkdir(parents=True, exist_ok=True)
    (converted / "model.bin").write_bytes(b"converted")
    (converted / CONVERSION_MARKER).write_text("marian\n", encoding="utf-8")
    return converted


def test_a_conversion_that_did_not_finish_is_not_a_cache(tmp_path: Path) -> None:
    """`model.bin` existing is not a finished conversion.

    Ctrl-C, an OOM kill or a full disk leaves a partial file with the right name, and a cache
    keyed on that name serves it to every later run. Nothing downstream catches it: the fast
    path returns before any digest is checked, and no digest for a converted model exists to
    check — the pin covers the publisher's archive, and the conversion is this project's own
    output. Reproduced on a real cache before this was written: a one-byte `model.bin` was
    returned as ready to use in place of 79.9 MB.
    """
    store, artefact, _ = prepared_store(tmp_path)
    interrupted = store.converted_dir(artefact)
    interrupted.mkdir(parents=True, exist_ok=True)
    (interrupted / "model.bin").write_bytes(b"\x00")

    assert not store._is_converted(interrupted)


def test_a_cache_from_before_the_marker_is_converted_once_more(tmp_path: Path) -> None:
    """The migration, stated as a test. Every cache built before this rule looks exactly like
    an interrupted one, so it is rebuilt — a one-off conversion, and the price of no longer
    trusting a directory nobody checked."""
    store, artefact, _ = prepared_store(tmp_path)
    older = store.converted_dir(artefact)
    older.mkdir(parents=True, exist_ok=True)
    (older / "model.bin").write_bytes(b"a complete conversion, from before the marker")

    assert not store._is_converted(older)


def test_the_marker_alone_is_not_a_cache_either(tmp_path: Path) -> None:
    """Both, so that a stray marker cannot stand in for the model."""
    store, artefact, _ = prepared_store(tmp_path)
    marked = store.converted_dir(artefact)
    marked.mkdir(parents=True, exist_ok=True)
    (marked / CONVERSION_MARKER).write_text("marian\n", encoding="utf-8")

    assert not store._is_converted(marked)


def test_a_failed_conversion_leaves_nothing_behind(tmp_path: Path) -> None:
    """Not even the staging directory: a `.partial` left in the cache is a directory the next
    run has to reason about, and the next run should find the cache empty."""
    store, artefact, _ = prepared_store(tmp_path)
    converted = store.converted_dir(artefact)

    with pytest.raises(TranslationArtifactError, match="could not convert"):
        store._convert(store.source_dir(artefact), converted)

    assert not converted.exists()
    assert not converted.with_name(converted.name + ".partial").exists()


# ---------------------------------------------------------------------------------------
# From the choice to the loader
#
# `open_translator` forwards the two overrides that exist for `scripts/measure_translation.py`
# — the beam width ADR 0009 traded away and the thread count ADR 0014 measured. Nothing
# asserted that they arrive. A mutation dropping them survived every test in this file, and a
# measurement labelled "beam 6" would have reported greedy numbers into an ADR.
# ---------------------------------------------------------------------------------------


class LoadedWith:
    """Stands in for a loaded model, holding the keyword arguments it was built with."""

    def __init__(self, **extra: int) -> None:
        self.extra = extra


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_ensure(self: object, artefact: object) -> tuple[Path, Path]:
        return tmp_path / "converted", tmp_path / "marian"

    def fake_load(
        converted: Path,
        spm: Path,
        *,
        source_language: str,
        target_language: str,
        **extra: int,
    ) -> LoadedWith:
        return LoadedWith(**extra)

    monkeypatch.setattr(TranslationModelStore, "ensure", fake_ensure)
    monkeypatch.setattr("on_the_fly.infrastructure.translation.opus_mt.load", fake_load)


@pytest.mark.usefixtures("loader")
def test_the_overrides_reach_the_loader(tmp_path: Path) -> None:
    translator = open_translator(
        resolve_engine(("en", "ru")), tmp_path, beam_size=6, intra_threads=3
    )

    assert isinstance(translator, LoadedWith)
    assert translator.extra == {"beam_size": 6, "intra_threads": 3}


@pytest.mark.usefixtures("loader")
def test_the_application_passes_neither_and_gets_the_shipped_defaults(tmp_path: Path) -> None:
    """Absent rather than `None`: the loader's own defaults are the shipped settings, and
    passing `None` through would override them with nothing."""
    translator = open_translator(resolve_engine(("en", "ru")), tmp_path)

    assert isinstance(translator, LoadedWith)
    assert translator.extra == {}
