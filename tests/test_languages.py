"""Tests for the supported-language registry.

The registry exists so the application cannot describe Tajik the way it describes English.
These tests pin that distinction, because losing it is the failure that matters: a user told
their language is "supported" will reasonably expect it to work like the others.
"""

from __future__ import annotations

import pytest

from on_the_fly.domain.languages import (
    SUPPORTED,
    Language,
    RecognitionTier,
    batch_languages,
    resolve,
    streaming_languages,
)


def test_all_eight_requested_languages_are_present() -> None:
    assert set(SUPPORTED) == {"en", "ru", "es", "it", "fr", "pt", "de", "tg"}


def test_seven_languages_stream_and_tajik_does_not() -> None:
    """The evidence in ADR 0007, expressed as a test."""
    streaming = {lang.code for lang in streaming_languages()}
    batch = {lang.code for lang in batch_languages()}

    assert streaming == {"en", "ru", "es", "it", "fr", "pt", "de"}
    assert batch == {"tg"}


def test_tajik_carries_its_caveat() -> None:
    """A tier alone is not enough; the reason has to travel with it."""
    tajik = resolve("tg")

    assert tajik.tier is RecognitionTier.BATCH
    assert tajik.has_caveat
    assert "no streaming model" in tajik.note
    assert "unverified" in tajik.note


def test_streaming_languages_carry_no_caveat() -> None:
    assert all(not lang.has_caveat for lang in streaming_languages())


def test_an_unsupported_language_is_refused() -> None:
    """Guessing produces confident nonsense, which is worse than an error."""
    with pytest.raises(KeyError, match="unsupported language"):
        resolve("zh")


def test_lookup_is_forgiving_about_case_and_spacing() -> None:
    assert resolve(" EN ").code == "en"
    assert resolve("Tg").code == "tg"


def test_rendering_a_language_states_its_tier() -> None:
    assert "STREAMING" in str(resolve("de"))
    rendered = str(resolve("tg"))
    assert "BATCH" in rendered
    assert "no streaming model" in rendered


def test_a_language_is_immutable() -> None:
    """The registry is a decision, not a runtime setting."""
    with pytest.raises(AttributeError):
        resolve("en").tier = RecognitionTier.BATCH  # type: ignore[misc]


def test_the_tier_of_a_new_language_must_be_stated() -> None:
    with pytest.raises(TypeError):
        Language("xx", "Example")  # type: ignore[call-arg]
