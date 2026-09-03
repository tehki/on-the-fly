"""Tests for the supported-language registry.

The registry exists so the application cannot describe a barely-working language the way it
describes English. These tests pin that distinction, because losing it is the failure that
matters: a user told their language is "supported" will reasonably expect it to work like
the others.

Tajik was the language that made the point, and ADR 0010 removed it. The distinction it
forced is still tested here — on a constructed language rather than a shipped one — so that
the machinery cannot rot away while no supported language needs it.
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


def test_the_seven_supported_languages_are_present() -> None:
    assert set(SUPPORTED) == {"en", "ru", "es", "it", "fr", "pt", "de"}


def test_every_supported_language_streams() -> None:
    """The evidence in ADR 0007 and the removal in ADR 0010, expressed as a test."""
    streaming = {lang.code for lang in streaming_languages()}
    batch = {lang.code for lang in batch_languages()}

    assert streaming == {"en", "ru", "es", "it", "fr", "pt", "de"}
    assert batch == set()


def test_tajik_is_no_longer_supported() -> None:
    """ADR 0010. Refusal is the point: it must not fall back to a worse tier silently."""
    with pytest.raises(KeyError, match="unsupported language"):
        resolve("tg")


def test_a_batch_language_still_carries_its_caveat() -> None:
    """A tier alone is not enough; the reason has to travel with it.

    No shipped language is BATCH today, so this is asserted on a constructed one. The
    machinery has to keep working for the language that needs it next.
    """
    example = Language(
        "xx",
        "Example",
        RecognitionTier.BATCH,
        note="no streaming model exists; accuracy is unverified",
    )

    assert not example.is_streaming
    assert example.has_caveat
    assert "no streaming model" in str(example)
    assert "BATCH" in str(example)


def test_streaming_languages_carry_no_caveat() -> None:
    assert all(not lang.has_caveat for lang in streaming_languages())


def test_an_unsupported_language_is_refused() -> None:
    """Guessing produces confident nonsense, which is worse than an error."""
    with pytest.raises(KeyError, match="unsupported language"):
        resolve("zh")


def test_lookup_is_forgiving_about_case_and_spacing() -> None:
    assert resolve(" EN ").code == "en"
    assert resolve("De").code == "de"


def test_rendering_a_language_states_its_tier() -> None:
    assert "STREAMING" in str(resolve("de"))


def test_a_language_is_immutable() -> None:
    """The registry is a decision, not a runtime setting."""
    with pytest.raises(AttributeError):
        resolve("en").tier = RecognitionTier.BATCH  # type: ignore[misc]


def test_the_tier_of_a_new_language_must_be_stated() -> None:
    with pytest.raises(TypeError):
        Language("xx", "Example")  # type: ignore[call-arg]
