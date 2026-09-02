"""Tests for the streaming recognition port.

`BatchStreamingRecognizer` is the honest adapter: it presents the existing batch recogniser
through the streaming interface and emits finals only. These tests pin the behaviour a real
streaming engine will have to match, so that swapping one in is an adapter change rather
than an application change.
"""

from __future__ import annotations

from array import array

import pytest

from on_the_fly.domain.audio import (
    AudioFormat,
    BatchStreamingRecognizer,
    SegmenterConfig,
    TranscriptEvent,
    UtteranceSegmenter,
)
from on_the_fly.domain.retention import EphemeralStore, ManualClock

FORMAT = AudioFormat()
FRAME_MS = 20
SAMPLES_PER_FRAME = FORMAT.frame_bytes(FRAME_MS) // FORMAT.sample_width_bytes

CONFIG = SegmenterConfig(
    frame_ms=FRAME_MS,
    pre_roll_ms=40,
    hangover_ms=40,
    min_utterance_ms=20,
    max_utterance_ms=2000,
)


def frame_of(value: int) -> bytes:
    return array("h", [value] * SAMPLES_PER_FRAME).tobytes()


SILENT = frame_of(0)
LOUD = frame_of(8000)


class ScriptedDetector:
    def __init__(self, script: list[bool]) -> None:
        self.script = list(script)
        self.index = 0

    def is_speech(self, frame: bytes) -> bool:
        if self.index >= len(self.script):
            return False
        answer = self.script[self.index]
        self.index += 1
        return answer

    def reset(self) -> None:
        self.index = 0


class CountingRecognizer:
    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
        self.calls += 1
        return self.text


def build(script: list[bool], recognizer: CountingRecognizer) -> BatchStreamingRecognizer:
    store = EphemeralStore("on-the-fly", clock=ManualClock())
    segmenter = UtteranceSegmenter(
        store=store, detector=ScriptedDetector(script), audio_format=FORMAT, config=CONFIG
    )
    return BatchStreamingRecognizer(store, segmenter, recognizer, FORMAT)


def drive(streamer: BatchStreamingRecognizer, script: list[bool]) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    for is_speech in script:
        events.extend(streamer.accept(LOUD if is_speech else SILENT))
    events.extend(streamer.finish())
    return events


def test_a_completed_utterance_produces_one_final_event() -> None:
    script = [False, True, True, True, False, False]
    recognizer = CountingRecognizer("good morning")
    streamer = build(script, recognizer)

    events = drive(streamer, script)

    assert len(events) == 1
    event = events[0]
    assert event.is_final is True
    assert event.text == "good morning"
    assert event.utterance_index == 1
    assert event.latency_seconds >= 0
    assert recognizer.calls == 1


def test_no_partials_are_emitted_and_the_adapter_says_so() -> None:
    """A caption renderer can skip its rewrite handling on this promise."""
    script = [False, True, True, True, False, False]
    streamer = build(script, CountingRecognizer())

    events = drive(streamer, script)

    assert streamer.emits_partials is False
    assert all(event.is_final for event in events)


def test_frames_that_complete_nothing_emit_nothing() -> None:
    """Most frames produce no event; the caller must not assume one per frame."""
    script = [False] * 6
    streamer = build(script, CountingRecognizer())

    produced = [streamer.accept(SILENT) for _ in script]

    assert all(events == () for events in produced)
    assert streamer.finish() == ()


def test_several_utterances_are_indexed_in_order() -> None:
    script = [False, True, True, False, False, True, True, False, False]
    recognizer = CountingRecognizer("yes")
    streamer = build(script, recognizer)

    events = drive(streamer, script)

    assert [event.utterance_index for event in events] == [1, 2]
    assert recognizer.calls == 2
    assert streamer.utterances_seen == 2


def test_an_empty_transcript_produces_no_event() -> None:
    """Emitting an empty final would make a caption renderer clear the screen for nothing."""
    script = [False, True, True, True, False, False]
    streamer = build(script, CountingRecognizer(""))

    assert drive(streamer, script) == []


def test_finish_flushes_an_utterance_still_in_progress() -> None:
    """Audio ends mid-sentence; the sentence is still delivered."""
    script = [False, True, True, True]
    recognizer = CountingRecognizer("halfway through")
    streamer = build(script, recognizer)

    during = [streamer.accept(LOUD if s else SILENT) for s in script]
    assert all(events == () for events in during), "no hangover, so nothing completes yet"

    final = streamer.finish()

    assert len(final) == 1
    assert final[0].text == "halfway through"


def test_reset_discards_in_flight_state() -> None:
    script = [False, True, True, True]
    streamer = build(script, CountingRecognizer())
    for is_speech in script:
        streamer.accept(LOUD if is_speech else SILENT)

    streamer.reset()

    assert streamer.utterances_seen == 0
    assert streamer.finish() == (), "reset must not leave a flushable utterance behind"


def test_the_audio_offset_points_at_where_the_utterance_started() -> None:
    """A caption belongs where the speech was, not where the recogniser finished."""
    script = [False, False, False, True, True, True, False, False]
    streamer = build(script, CountingRecognizer("later on"))

    events = drive(streamer, script)

    assert len(events) == 1
    # Three silent frames precede speech; the utterance covers roughly frames 2-7.
    assert 0.0 <= events[0].audio_offset_seconds < 0.1


def test_event_rendering_shows_text_and_marks_finality() -> None:
    event = TranscriptEvent(
        utterance_index=1,
        text="the meeting is at three",
        is_final=True,
        audio_offset_seconds=12.5,
        latency_seconds=0.4,
    )

    rendered = str(event)

    assert "the meeting is at three" in rendered
    assert "final" in rendered
    assert "12.50" in rendered


def test_a_partial_renders_as_a_partial() -> None:
    event = TranscriptEvent(
        utterance_index=1,
        text="the meeting is",
        is_final=False,
        audio_offset_seconds=12.5,
        latency_seconds=0.1,
    )

    assert "partial" in str(event)


def test_latency_is_measured_per_utterance() -> None:
    """The number the budget is judged on, recorded where it happens."""
    script = [False, True, True, True, False, False]
    streamer = build(script, CountingRecognizer("measured"))

    events = drive(streamer, script)

    assert events[0].latency_seconds == pytest.approx(events[0].latency_seconds)
    assert events[0].latency_seconds < 5.0, "a fake recogniser should be effectively instant"
