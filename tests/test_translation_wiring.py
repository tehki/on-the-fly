"""Tests for translation wired into the streaming path (ADR 0009).

A fake translator stands in for CTranslate2, so the wiring — which events get translated,
where the translated text goes, what happens when translation fails — is asserted in
milliseconds and without an 80 MB model.

The behaviour most worth pinning is the least visible one: a translation is `EPHEMERAL`
project content the moment it exists, so it goes into the retention store rather than
living as a local variable that nothing accounts for.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Sequence
from pathlib import Path

import pytest

from on_the_fly.app import TranslatedEvent, build_store, translate_finals
from on_the_fly.app.cli import main
from on_the_fly.domain.audio import TranscriptEvent
from on_the_fly.domain.retention import EphemeralStore

RATE = 16_000


def speech_wav(path: Path, seconds: float = 1.0) -> Path:
    count = int(RATE * seconds)
    samples = [int(8000 * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(count)]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def event(text: str, *, is_final: bool, index: int = 0) -> TranscriptEvent:
    return TranscriptEvent(
        utterance_index=index,
        text=text,
        is_final=is_final,
        audio_offset_seconds=0.0,
        latency_seconds=0.0,
    )


class FakeTranslator:
    """Records what it was asked to translate. Optionally refuses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.seen: list[tuple[str, str, str]] = []
        self._fail = fail

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        self.seen.append((text, source_language, target_language))
        if self._fail:
            raise RuntimeError("engine unavailable")
        return f"[{target_language}] {text}"


def drain(
    events: Sequence[TranscriptEvent],
    translator: FakeTranslator,
    *,
    source_language: str = "en",
    target_language: str = "ru",
    store: EphemeralStore | None = None,
) -> list[TranslatedEvent]:
    return list(
        translate_finals(
            events,
            translator,
            source_language=source_language,
            target_language=target_language,
            store=store,
        )
    )


# --------------------------------------------------------------------------------------
# Which events get translated
# --------------------------------------------------------------------------------------


def test_only_finals_are_translated() -> None:
    """ADR 0009's decision, as a test. Sixteen partials would be sixteen inferences."""
    translator = FakeTranslator()

    results = drain(
        [
            event("hello", is_final=False),
            event("hello there", is_final=False),
            event("hello there friend", is_final=True),
        ],
        translator,
    )

    assert [r.translation for r in results] == [None, None, "[ru] hello there friend"]
    assert [seen[0] for seen in translator.seen] == ["hello there friend"]


def test_partials_still_reach_the_caller() -> None:
    """The source caption keeps streaming; only the translation waits for the endpoint."""
    results = drain([event("half a sen", is_final=False)], FakeTranslator())

    assert [r.event.text for r in results] == ["half a sen"]
    assert results[0].translation is None


def test_the_language_pair_is_passed_through() -> None:
    translator = FakeTranslator()

    drain([event("hello", is_final=True)], translator, source_language="en", target_language="ru")

    assert translator.seen == [("hello", "en", "ru")]


def test_translation_latency_is_recorded_for_finals_only() -> None:
    results = drain(
        [event("partial", is_final=False), event("final", is_final=True)], FakeTranslator()
    )

    assert results[0].translation_seconds is None
    assert results[1].translation_seconds is not None
    assert results[1].translation_seconds >= 0.0


# --------------------------------------------------------------------------------------
# Retention. A translation is EPHEMERAL from the moment it exists.
# --------------------------------------------------------------------------------------


def test_a_translation_is_placed_in_the_retention_store() -> None:
    store = build_store("test-translation")

    drain([event("hello there friend", is_final=True)], FakeTranslator(), store=store)

    assert len(store) == 1


def test_partials_put_nothing_in_the_store() -> None:
    store = build_store("test-translation")

    drain([event("half a sen", is_final=False)], FakeTranslator(), store=store)

    assert len(store) == 0


def test_an_empty_translation_is_not_stored() -> None:
    """Storing an empty string would account for content that does not exist."""

    class Empty(FakeTranslator):
        def translate(self, text: str, *, source_language: str, target_language: str) -> str:
            return ""

    store = build_store("test-translation")

    results = drain([event("hello", is_final=True)], Empty(), store=store)

    assert results[0].translation is None
    assert len(store) == 0


# --------------------------------------------------------------------------------------
# Failure. Losing the translation must not lose the transcript too.
# --------------------------------------------------------------------------------------


def test_a_translation_failure_still_yields_the_transcript() -> None:
    """The caption was working. A broken translator must not take it away as well."""
    results = drain([event("hello there friend", is_final=True)], FakeTranslator(fail=True))

    assert results[0].event.text == "hello there friend"
    assert results[0].translation is None


def test_a_translation_failure_stores_nothing() -> None:
    store = build_store("test-translation")

    drain([event("hello", is_final=True)], FakeTranslator(fail=True), store=store)

    assert len(store) == 0


def test_a_failure_does_not_end_the_stream() -> None:
    translator = FakeTranslator(fail=True)

    results = drain(
        [event("one", is_final=True), event("two", is_final=True)],
        translator,
    )

    assert len(results) == 2
    assert len(translator.seen) == 2


def test_translated_event_reports_finality_of_the_event_it_wraps() -> None:
    assert TranslatedEvent(event("x", is_final=True)).is_final
    assert not TranslatedEvent(event("x", is_final=False)).is_final


# --------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------


def test_translating_into_the_source_language_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(
        [
            "stream",
            str(path),
            "--language",
            "en",
            "--translate-to",
            "en",
            "--cache-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "nothing to translate" in capsys.readouterr().err


def test_an_unknown_target_language_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--translate-to", "zz", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert "unsupported language" in capsys.readouterr().err


def test_a_pair_with_no_pinned_model_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """German is a supported language with no translation model pinned for it."""
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--translate-to", "de", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert "no pinned translation model" in capsys.readouterr().err
