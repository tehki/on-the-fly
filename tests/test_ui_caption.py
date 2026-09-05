"""Tests for what the window shows (ADR 0016).

No Qt, no display. The decisions worth testing in a live translator's interface are about
what replaces what and what is deliberately not kept, and those live in `CaptionModel`.

The retention tests are the ones that matter. A history pane is the easiest feature in this
application to add by accident — appending instead of replacing is a one-character
difference — and it would break the promise `docs/RETENTION_POLICY.md` makes.
"""

from __future__ import annotations

from on_the_fly.domain.audio.levels import InputQuality
from on_the_fly.ui.caption import Caption, CaptionModel, Status


def listening_model() -> CaptionModel:
    model = CaptionModel()
    model.starting()
    model.listening()
    return model


# --------------------------------------------------------------------------------------
# No scrollback. The point of the design.
# --------------------------------------------------------------------------------------


def test_a_new_utterance_replaces_the_previous_one() -> None:
    """Live translation is fine; scrollback is not (docs/RETENTION_POLICY.md)."""
    model = listening_model()

    model.final("the first thing that was said")
    model.translated("перевод первого")
    state = model.final("the second thing that was said")

    assert state.caption.source == "the second thing that was said"
    assert state.caption.translation is None, "the previous translation must not linger"


def test_the_model_holds_exactly_one_caption() -> None:
    """Structural, not behavioural: there is nowhere for a history to accumulate."""
    model = listening_model()
    for i in range(20):
        model.final(f"utterance {i}")

    assert isinstance(model.state.caption, Caption)
    assert model.state.caption.source == "utterance 19"


def test_stopping_clears_the_caption() -> None:
    """The last thing someone said, left on screen after they stop, is scrollback of one."""
    model = listening_model()
    model.final("something private")
    model.translated("что-то личное")

    state = model.stopped()

    assert state.caption.is_empty
    assert state.caption.translation is None


def test_failure_clears_the_caption_too() -> None:
    """The text is stale and the reason matters more than the words."""
    model = listening_model()
    model.final("half a sentence")

    state = model.failed("the audio device stopped delivering audio")

    assert state.caption.is_empty
    assert "device" in state.detail


def test_changing_languages_clears_the_caption() -> None:
    """A caption from one pair must not sit under a heading claiming another."""
    model = listening_model()
    model.final("good morning")

    state = model.set_languages(source="ru", target="en")

    assert state.caption.is_empty
    assert state.source_language == "ru"


# --------------------------------------------------------------------------------------
# Partials and finals
# --------------------------------------------------------------------------------------


def test_a_partial_replaces_the_previous_partial() -> None:
    model = listening_model()

    model.partial("so I was")
    state = model.partial("so I was thinking")

    assert state.caption.source == "so I was thinking"
    assert state.caption.is_final is False


def test_a_partial_carries_no_translation() -> None:
    """ADR 0009: partials are never translated."""
    state = listening_model().partial("half a sen")

    assert state.caption.translation is None


def test_a_translation_attaches_to_a_final() -> None:
    model = listening_model()
    model.final("good morning")

    state = model.translated("доброе утро")

    assert state.caption.translation == "доброе утро"
    assert state.caption.is_final is True


def test_a_translation_is_refused_for_a_partial() -> None:
    """Displaying a translation of text that is about to change is worse than waiting."""
    model = listening_model()
    model.partial("good mor")

    state = model.translated("доброе утро")

    assert state.caption.translation is None


def test_a_partial_after_a_final_starts_the_next_utterance_clean() -> None:
    model = listening_model()
    model.final("first sentence")
    model.translated("первое предложение")

    state = model.partial("second")

    assert state.caption.source == "second"
    assert state.caption.translation is None


# --------------------------------------------------------------------------------------
# Lifecycle and controls
# --------------------------------------------------------------------------------------


def test_a_fresh_model_is_idle_and_startable() -> None:
    model = CaptionModel()

    assert model.state.status is Status.IDLE
    assert model.state.can_start is True
    assert model.state.can_stop is False


def test_starting_disables_starting_again() -> None:
    """Two pipelines competing for one microphone is not a state worth supporting."""
    model = CaptionModel()

    state = model.starting()

    assert state.can_start is False
    assert state.can_stop is True
    assert state.is_busy is True


def test_listening_is_not_busy() -> None:
    """Busy means a transition is in flight, not that audio is flowing."""
    assert listening_model().state.is_busy is False


def test_a_failed_run_can_be_started_again() -> None:
    """A device that was unplugged may be plugged back in."""
    model = listening_model()

    state = model.failed("device disconnected")

    assert state.can_start is True
    assert state.can_stop is False


def test_starting_clears_a_caption_left_by_a_previous_run() -> None:
    model = listening_model()
    model.final("from the last session")
    model.stopped()

    state = model.starting()

    assert state.caption.is_empty


# --------------------------------------------------------------------------------------
# Honesty
# --------------------------------------------------------------------------------------


def test_dropped_audio_is_reported_rather_than_hidden() -> None:
    """Lost audio is a dropped word. The user is told, not protected from it."""
    model = listening_model()

    state = model.note_overflow(3)

    assert state.overflow_count == 3


def test_an_unusable_microphone_is_reported_rather_than_transcribed_silently() -> None:
    """Clipped audio produces fluent words nobody said; the user cannot see that (ADR 0019)."""
    model = listening_model()

    state = model.note_input_quality(InputQuality.CLIPPING)

    assert state.input_quality is InputQuality.CLIPPING
    assert "gain" in state.input_quality.advice


def test_the_input_verdict_clears_when_the_run_stops() -> None:
    """A verdict about a device that is no longer open describes nothing."""
    model = listening_model()
    model.note_input_quality(InputQuality.CLIPPING)

    assert model.stopped().input_quality is InputQuality.OK


def test_a_usable_microphone_says_nothing() -> None:
    assert listening_model().state.input_quality.advice == ""


def test_the_overflow_count_resets_when_the_run_stops() -> None:
    model = listening_model()
    model.note_overflow(5)

    assert model.stopped().overflow_count == 0


def test_attribution_survives_across_utterances() -> None:
    """CC-BY-4.0 obliges a notice a user can reach; it must not scroll away with a caption."""
    model = listening_model()
    model.set_attribution("OPUS-MT (Helsinki-NLP), CC-BY-4.0")

    model.final("something")
    model.final("something else")

    assert "Helsinki-NLP" in model.state.attribution
