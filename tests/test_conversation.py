"""Tests for hearing two languages at once (ADR 0047).

The identification rests on a comparison ADR 0043 could not make absolutely: not *is this
confidence good enough*, which has no threshold that works, but *which of these models scored
better on the same audio*, which needs no threshold at all.

What is asserted here is the decision, against fake recognisers. Whether the decision is
*right* about real speech is a measurement and lives in `docs/PERFORMANCE_BUDGET.md`: five
published clips, three recognisers each, five identified correctly.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from on_the_fly.app.pipeline import build_store, translate_conversation
from on_the_fly.domain.audio import AudioFormat, TranscriptEvent
from on_the_fly.infrastructure.asr.conversation import ConversationRecognizer
from on_the_fly.infrastructure.translation import TranslationError

FRAME = b"\x00\x00" * 160


class Scripted:
    """A recogniser that emits whatever it was told to, one batch per frame."""

    def __init__(self, batches: list[list[TranscriptEvent]]) -> None:
        self._batches = list(batches)
        self.resets = 0
        self.warmed = 0
        self.validated = 0

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        return tuple(self._batches.pop(0)) if self._batches else ()

    def finish(self) -> Sequence[TranscriptEvent]:
        return ()

    def reset(self) -> None:
        self.resets += 1

    def warm_up(self) -> None:
        self.warmed += 1

    def validate_format(self, audio_format: AudioFormat) -> None:
        self.validated += 1


def final(text: str, confidence: float | None) -> TranscriptEvent:
    return TranscriptEvent(
        utterance_index=1,
        text=text,
        is_final=True,
        audio_offset_seconds=0.0,
        latency_seconds=0.0,
        confidence=confidence,
    )


def partial(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        utterance_index=1,
        text=text,
        is_final=False,
        audio_offset_seconds=0.0,
        latency_seconds=0.0,
    )


def heard(**scripts: list[list[TranscriptEvent]]) -> ConversationRecognizer:
    return ConversationRecognizer({code: Scripted(batches) for code, batches in scripts.items()})


# --------------------------------------------------------------------------------------
# Which language spoke
# --------------------------------------------------------------------------------------


def test_the_model_that_understood_it_wins() -> None:
    """The measured shape: both produce a final, and one is far more sure of itself."""
    conversation = heard(
        en=[[final("sounds like english", -1.11)]],
        fr=[[final("ce dernier a évolué", -0.44)]],
    )

    events = conversation.accept(FRAME)

    assert [event.text for event in events] == ["ce dernier a évolué"]
    assert conversation.language == "fr"


def test_a_model_with_nothing_to_say_is_out_before_any_comparison() -> None:
    """The French model produces no output whatever on English speech. Silence is not a low
    score; it is a model saying it has nothing to offer."""
    conversation = heard(fr=[[]], en=[[final("after early nightfall", -0.25)]])

    events = conversation.accept(FRAME)

    assert [event.text for event in events] == ["after early nightfall"]
    assert conversation.language == "en"


def test_every_event_says_which_language_recognised_it() -> None:
    """Which is what lets a caller translate an utterance into whatever the *other* person
    is speaking."""
    conversation = heard(en=[[final("hello", -0.2)]], fr=[[final("bonjour", -0.9)]])

    assert [event.language for event in conversation.accept(FRAME)] == ["en"]


def test_a_close_call_keeps_the_language_already_being_spoken() -> None:
    """A conversation does not usually change language mid-sentence, and a coin toss between
    two near-equal scores would."""
    conversation = heard(
        en=[[final("first", -0.30)], [final("second", -0.52)]],
        fr=[[], [final("deuxième", -0.50)]],
    )
    conversation.accept(FRAME)
    assert conversation.language == "en"

    events = conversation.accept(FRAME)

    assert conversation.language == "en", "a 0.02 margin changed the speaker"
    assert [event.text for event in events] == ["second"]


def test_a_clear_win_does_change_the_language() -> None:
    """Otherwise the first speaker owns the conversation."""
    conversation = heard(
        en=[[final("first", -0.30)], [final("nonsense", -1.20)]],
        fr=[[], [final("réponse", -0.40)]],
    )
    conversation.accept(FRAME)

    events = conversation.accept(FRAME)

    assert conversation.language == "fr"
    assert [event.text for event in events] == ["réponse"]


def test_a_final_with_no_confidence_still_decides_something() -> None:
    """An older sherpa-onnx reports no probabilities at all (ADR 0043). The run should
    continue in the language that is speaking rather than stop."""
    conversation = heard(en=[[final("no numbers", None)]], fr=[[]])

    events = conversation.accept(FRAME)

    assert [event.text for event in events] == ["no numbers"]
    assert conversation.language == "en"


# --------------------------------------------------------------------------------------
# What reaches the screen in between
# --------------------------------------------------------------------------------------


def test_partials_come_from_the_language_that_spoke_last() -> None:
    """Nothing has finished, so there is nothing to judge — and three competing captions
    would be worse than one that is occasionally a sentence behind."""
    conversation = heard(
        en=[[final("first", -0.9)], [partial("keeps go")]],
        fr=[[final("premier", -0.3)], [partial("continue")]],
    )
    conversation.accept(FRAME)

    events = conversation.accept(FRAME)

    assert [event.text for event in events] == ["continue"]
    assert all(not event.is_final for event in events)


def test_the_first_utterance_is_judged_like_any_other() -> None:
    """Only its partials are a guess."""
    conversation = heard(en=[[partial("gu")]], fr=[[partial("de")]])

    assert [event.text for event in conversation.accept(FRAME)] == ["gu"]
    assert conversation.language == "en", "the first configured language starts"


# --------------------------------------------------------------------------------------
# The shape of the thing
# --------------------------------------------------------------------------------------


def test_one_language_is_not_a_conversation() -> None:
    with pytest.raises(ValueError, match="at least two"):
        ConversationRecognizer({"en": Scripted([])})


def test_warming_up_warms_all_of_them() -> None:
    """Loading is paid before the clock starts, for every model that is listening."""
    recognisers = {"en": Scripted([]), "fr": Scripted([])}
    ConversationRecognizer(recognisers).warm_up()

    assert [scripted.warmed for scripted in recognisers.values()] == [1, 1]


def test_the_format_is_checked_against_all_of_them() -> None:
    recognisers = {"en": Scripted([]), "fr": Scripted([])}
    ConversationRecognizer(recognisers).validate_format(AudioFormat())

    assert [scripted.validated for scripted in recognisers.values()] == [1, 1]


def test_resetting_resets_all_of_them_and_forgets_who_was_speaking() -> None:
    recognisers = {"en": Scripted([[final("x", -0.9)]]), "fr": Scripted([[final("y", -0.2)]])}
    conversation = ConversationRecognizer(recognisers)
    conversation.accept(FRAME)
    assert conversation.language == "fr"

    conversation.reset()

    assert [scripted.resets for scripted in recognisers.values()] == [1, 1]
    assert conversation.language == "en"


def test_it_says_which_languages_are_listening() -> None:
    assert heard(en=[], fr=[], ru=[]).languages == ("en", "fr", "ru")


# --------------------------------------------------------------------------------------
# Translating into whichever language the other person is speaking
# --------------------------------------------------------------------------------------


class Echoing:
    """A translator that says which direction it was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        self.calls.append((text, source_language, target_language))
        return f"{text} in {target_language}"


class Refusing:
    """A translator that fails, which is a thing a real one does."""

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        raise TranslationError("no")


def spoken(text: str, language: str | None, *, is_final: bool = True) -> TranscriptEvent:
    return TranscriptEvent(
        utterance_index=1,
        text=text,
        is_final=is_final,
        audio_offset_seconds=0.0,
        latency_seconds=0.0,
        language=language,
    )


def test_each_utterance_goes_to_the_language_the_other_person_speaks() -> None:
    """The whole point: the direction is chosen per utterance, not once for the run."""
    translators = {("en", "ru"): Echoing(), ("ru", "en"): Echoing()}

    out = list(translate_conversation([spoken("hello", "en"), spoken("привет", "ru")], translators))

    assert [item.translation for item in out] == ["hello in ru", "привет in en"]
    assert [item.target_language for item in out] == ["ru", "en"]


def test_a_partial_is_never_translated() -> None:
    """ADR 0009, unchanged by there being two languages to translate into."""
    translator = Echoing()

    out = list(
        translate_conversation(
            [spoken("hel", "en", is_final=False)],
            {("en", "ru"): translator},
        )
    )

    assert out[0].translation is None
    assert translator.calls == []


def test_an_utterance_nobody_claimed_passes_through_untranslated() -> None:
    """`language` is `None` from a single-language run, which has its own translator."""
    out = list(
        translate_conversation(
            [spoken("hello", None)],
            {("en", "ru"): Echoing()},
        )
    )

    assert out[0].translation is None
    assert out[0].target_language is None


def test_a_direction_with_no_translator_still_shows_the_caption() -> None:
    """The half that was working keeps working."""
    out = list(
        translate_conversation(
            [spoken("привет", "ru")],
            {("en", "ru"): Echoing()},
        )
    )

    assert out[0].event.text == "привет"
    assert out[0].translation is None


def test_a_translation_failure_does_not_end_the_conversation() -> None:
    translators = {("en", "ru"): Refusing(), ("ru", "en"): Echoing()}

    out = list(
        translate_conversation(
            [spoken("hello", "en"), spoken("привет", "ru")],
            translators,  # type: ignore[arg-type]
        )
    )

    assert out[0].translation is None, "a failure was reported as a translation"
    assert out[1].translation == "привет in en", "the run stopped at the first failure"


def test_a_translation_is_ephemeral_the_moment_it_exists() -> None:
    """Same store, same ten-second rule as the audio it came from."""
    store = build_store("test-conversation")

    out = list(
        translate_conversation(
            [spoken("hello", "en")],
            {("en", "ru"): Echoing()},
            store=store,
        )
    )

    assert out[0].translation is not None
    assert len(store) == 1
    store.purge_all()


def test_how_long_a_translation_took_is_reported() -> None:
    out = list(
        translate_conversation(
            [spoken("hello", "en")],
            {("en", "ru"): Echoing()},
        )
    )

    assert out[0].translation_seconds is not None
    assert out[0].translation_seconds >= 0.0
