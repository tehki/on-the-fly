"""Tests for the streaming path through the composition root and the command line.

A fake streaming recogniser stands in for sherpa-onnx so the wiring — event flow, transcript
retention, language refusal, cleanup — is asserted in milliseconds and without a 73 MB model.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from on_the_fly.app import StreamingRun
from on_the_fly.app.cli import _nothing_recognised_lines, main
from on_the_fly.app.pipeline import StreamingStats
from on_the_fly.domain import languages
from on_the_fly.domain.audio import AudioFormat, TranscriptEvent
from on_the_fly.domain.languages import Language, RecognitionTier
from on_the_fly.domain.languages import resolve as resolve_language
from on_the_fly.domain.retention import ReapReport
from on_the_fly.infrastructure.audio import WavFileSource

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


class FakeStreamer:
    """Emits two partials then a final, on fixed frame counts."""

    def __init__(self) -> None:
        self.frames = 0
        self.reset_calls = 0
        self.warmed = False

    def warm_up(self) -> None:
        self.warmed = True

    def validate_format(self, audio_format: AudioFormat) -> None:
        return None

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        self.frames += 1
        if self.frames == 5:
            return (self._event("hello", is_final=False),)
        if self.frames == 10:
            return (self._event("hello there", is_final=False),)
        if self.frames == 20:
            return (self._event("hello there friend", is_final=True),)
        return ()

    def finish(self) -> Sequence[TranscriptEvent]:
        return ()

    def reset(self) -> None:
        self.reset_calls += 1

    def _event(self, text: str, *, is_final: bool) -> TranscriptEvent:
        return TranscriptEvent(
            utterance_index=1,
            text=text,
            is_final=is_final,
            audio_offset_seconds=0.0,
            latency_seconds=0.0,
        )


class CountingSource:
    """A source that records how often it was closed."""

    def __init__(self, frames: int = 40) -> None:
        self._frames = [bytes(640)] * frames
        self.closed = 0

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat()

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def close(self) -> None:
        self.closed += 1


def test_the_source_is_released_when_the_stream_ends() -> None:
    """A microphone left open after a session is a privacy problem, not just a leak.

    Added after mutation testing showed the suite passed with the close() removed — the
    batch path had this covered and the streaming path did not.
    """
    source = CountingSource()
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    assert source.closed >= 1


def test_the_source_is_released_when_the_caller_walks_away() -> None:
    source = CountingSource(frames=200)
    run = StreamingRun(source, FakeStreamer())

    generator = run.events()
    next(generator)
    generator.close()

    assert source.closed >= 1, "abandoning the stream must still release the device"


def test_events_are_yielded_as_they_appear(tmp_path: Path) -> None:
    """Yielded, not returned. A streaming result that arrives at the end is not streaming."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    events = list(run.events())

    assert [e.text for e in events] == ["hello", "hello there", "hello there friend"]
    assert [e.is_final for e in events] == [False, False, True]


def test_finals_are_stored_and_partials_are_not(tmp_path: Path) -> None:
    """A trail of half-sentences would retain more content than the finished text."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    assert len(run.final_handles) == 1, "one final stored, both partials dropped"


def test_the_store_is_purged_and_the_source_closed(tmp_path: Path) -> None:
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    stats = run.stats
    assert stats is not None
    assert stats.retention_clean
    assert stats.entries_remaining == 0
    assert len(run.store) == 0


def test_stats_report_pace_and_first_text(tmp_path: Path) -> None:
    source = WavFileSource(speech_wav(tmp_path / "a.wav", seconds=2.0))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    stats = run.stats
    assert stats is not None
    assert stats.frames_read == 100
    assert stats.audio_seconds == pytest.approx(2.0, abs=0.05)
    assert stats.partials == 2
    assert stats.finals == 1
    assert stats.first_text_after_seconds is not None
    assert stats.keeps_up, "a fake recogniser must comfortably keep up"


def test_abandoning_the_stream_still_cleans_up(tmp_path: Path) -> None:
    """The caller stops consuming; the source and the store are still released."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav", seconds=3.0))
    run = StreamingRun(source, FakeStreamer())

    generator = run.events()
    next(generator)
    generator.close()

    stats = run.stats
    assert stats is not None, "cleanup must run even when the caller walks away"
    assert stats.retention_clean
    assert len(run.store) == 0


# ======================================================================================
# Command line
# ======================================================================================


def patch_streaming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeStreamer:
    fake = FakeStreamer()
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: model_dir)
    monkeypatch.setattr("on_the_fly.app.cli.SherpaStreamingRecognizer", lambda *a, **k: fake)
    return fake


def test_the_cli_streams_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")
    fake = patch_streaming(monkeypatch, tmp_path)

    exit_code = main(["stream", str(path), "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "English (en, streaming)" in output
    assert "hello there friend" in output
    assert "keeps up" in output
    assert "retention     clean" in output
    assert fake.warmed, "the model is loaded before the clock starts"


def test_finals_only_hides_partials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")
    patch_streaming(monkeypatch, tmp_path)

    main(["stream", str(path), "--cache-dir", str(tmp_path), "--finals-only"])

    output = capsys.readouterr().out
    assert "hello there friend" in output
    # The summary line legitimately counts partials, so assert on the event marker rather
    # than the word: no partial event was rendered.
    assert "partial]" not in output
    assert "final  ]" in output


def test_a_batch_only_language_is_refused_not_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently giving batch latency to someone who asked to stream is worse than refusing.

    Tajik used to supply this case; ADR 0010 removed it, and no shipped language is BATCH
    today. The guard in the CLI is still live, so the language is injected rather than the
    test deleted — a control that stops being exercised is a control on its way out.
    """
    path = speech_wav(tmp_path / "a.wav")
    batch_only = Language(
        "xx",
        "Example",
        RecognitionTier.BATCH,
        note="no streaming model exists",
    )
    monkeypatch.setitem(languages.SUPPORTED, "xx", batch_only)

    exit_code = main(["stream", str(path), "--language", "xx", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not a streaming language" in captured.err
    assert "transcribe" in captured.err, "the error must say what to use instead"


def test_a_removed_language_is_refused_outright(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0010: Tajik is gone, and asking for it fails closed rather than guessing."""
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "tg", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unsupported language" in captured.err


def test_a_batch_language_is_refused_with_the_command_that_would_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """German cannot stream, and the refusal has to be useful to a user rather than to a
    maintainer.

    It used to answer with `pin one with scripts/pin_model.py`, which is advice for someone
    working on this repository. German is served in batch by the Whisper model `transcribe`
    already loads, so that is what the message names (ADR 0034).
    """
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "de", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "is not a streaming language" in captured.err
    assert "transcribe" in captured.err
    assert "pin_model.py" not in captured.err, "that is advice for a maintainer, not a user"
    # And it must not read as an equivalent. ADR 0035 measured what the batch engine returns
    # for a non-English language, and recommending it without that caveat would be the
    # over-claim this project keeps having to correct.
    assert "fallback rather than a substitute" in captured.err


def test_an_unknown_language_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "zh", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert "unsupported language" in capsys.readouterr().err


def test_the_recogniser_defaults_to_one_thread() -> None:
    """ADR 0014. More threads measured strictly slower at every core count, and four fell
    behind real time even with four cores — with byte-identical transcripts, so there is no
    quality trade to weigh against it."""
    from on_the_fly.app.cli import build_parser

    args = build_parser().parse_args(["stream", "x.wav"])

    assert args.threads == 1


# --------------------------------------------------------------------------------------
# The arithmetic the command line prints
#
# `real_time_factor`, `keeps_up` and `retention_clean` are three properties a reader takes
# at face value: "0.399x (keeps up)", "retention clean". Mutation testing found their
# comparisons could be flipped without a test noticing — including the one that decides
# whether a run is reported as keeping up at all.
# --------------------------------------------------------------------------------------


def stats(
    *, audio: float, wall: float, remaining: int = 0, reap: ReapReport | None = None
) -> StreamingStats:
    return StreamingStats(
        frames_read=1,
        audio_seconds=audio,
        wall_seconds=wall,
        partials=0,
        finals=0,
        first_text_after_seconds=None,
        final_reap=reap if reap is not None else ReapReport(),
        entries_remaining=remaining,
    )


def test_exactly_real_time_is_not_keeping_up() -> None:
    """`docs/PERFORMANCE_BUDGET.md` sets the target as "under 1.0x", and the boundary is
    where that matters: at exactly real time there is no margin, so any jitter puts the run
    behind with nothing held back to catch up from."""
    assert not stats(audio=2.0, wall=2.0).keeps_up
    assert stats(audio=2.0, wall=1.999).keeps_up


def test_a_run_with_no_audio_reports_no_pace_rather_than_dividing_by_zero() -> None:
    """Zero is the boundary of the guard, and a run that read no frames has no pace to
    report. A number here would be one nobody measured."""
    assert stats(audio=0.0, wall=1.0).real_time_factor == 0.0
    assert stats(audio=0.0, wall=0.0).keeps_up, "no audio is not a run that fell behind"

    # The guard is on zero, not on "less than a second". A clip shorter than one second
    # still has a pace, and a run that fell behind on one must say so.
    assert stats(audio=0.5, wall=1.0).real_time_factor == pytest.approx(2.0)


def test_content_still_held_is_not_clean_even_when_nothing_failed_to_delete() -> None:
    """Both clauses, and the one that can fail on its own.

    A reap that reported no failures says nothing about what was never due. Content still
    in the store at the end of a run is content retained past the point anyone needed it,
    which is the claim the exit code is derived from.
    """
    assert not stats(audio=1.0, wall=1.0, remaining=1).retention_clean
    assert stats(audio=1.0, wall=1.0, remaining=0).retention_clean


def test_a_failed_deletion_is_not_clean_even_with_the_store_empty() -> None:
    """The other clause on its own: the index is empty and a location refused to let go."""
    failed = ReapReport(failed=("entry",))

    assert not failed.ok
    assert not stats(audio=1.0, wall=1.0, remaining=0, reap=failed).retention_clean


def test_content_awaiting_a_retry_is_not_clean_either() -> None:
    """`pending_retry` means a deletion has not happened yet, not that it will not."""
    pending = ReapReport(pending_retry=("entry",))

    assert not pending.ok
    assert not stats(audio=1.0, wall=1.0, remaining=0, reap=pending).retention_clean


# ---------------------------------------------------------------------------------------
# Speech went in and nothing came out (ADR 0044)
#
# The one thing this project can say about the *model* rather than about the audio. Measured:
# the French model produces nothing at all on English speech — no partials, no finals — while
# the level monitor reports `ok` throughout. Silence and noise produce no speech seconds, so
# they cannot reach it.
# ---------------------------------------------------------------------------------------


class SilentRecognizer:
    """A streaming recogniser that hears nothing, whatever it is given."""

    def emits_partials(self) -> bool:
        return True

    def accept(self, frame: bytes) -> tuple[TranscriptEvent, ...]:
        return ()

    def finish(self) -> tuple[TranscriptEvent, ...]:
        return ()

    def reset(self) -> None: ...

    def validate_format(self, audio_format: AudioFormat) -> None: ...

    def warm_up(self) -> None: ...


class AlwaysSpeech:
    def is_speech(self, frame: bytes) -> bool:
        return True


class NeverSpeech:
    def is_speech(self, frame: bytes) -> bool:
        return False


def stats_for(tmp_path: Path, detector: object) -> StreamingStats:
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        SilentRecognizer(),
        detector=detector,  # type: ignore[arg-type]
    )
    list(run.events())
    stats = run.stats
    assert stats is not None
    return stats


def test_a_run_counts_how_much_of_it_was_speech(tmp_path: Path) -> None:
    stats = stats_for(tmp_path, AlwaysSpeech())

    assert stats.speech_seconds == pytest.approx(stats.audio_seconds)


def test_a_run_with_no_speech_in_it_counts_none(tmp_path: Path) -> None:
    assert stats_for(tmp_path, NeverSpeech()).speech_seconds == 0.0


def test_speech_with_no_text_is_reported(tmp_path: Path) -> None:
    lines = _nothing_recognised_lines(stats_for(tmp_path, AlwaysSpeech()), resolve_language("fr"))

    assert lines, "speech went in, nothing came out, and nothing was said about it"
    assert "none of it was recognised" in lines[0]
    assert "French" in lines[1], "the message must name the language that was asked for"


def test_silence_with_no_text_is_not_a_finding(tmp_path: Path) -> None:
    """Which is most of the recordings anyone will ever point this at."""
    assert (
        _nothing_recognised_lines(stats_for(tmp_path, NeverSpeech()), resolve_language("en")) == []
    )


def test_a_fraction_of_a_second_of_speech_is_not_enough_to_accuse_a_model(
    tmp_path: Path,
) -> None:
    """A frame or two of energy is not evidence that a model failed."""
    stats = replace(stats_for(tmp_path, NeverSpeech()), speech_seconds=0.2)

    assert _nothing_recognised_lines(stats, resolve_language("en")) == []


def test_text_that_was_recognised_is_never_a_finding(tmp_path: Path) -> None:
    stats = replace(stats_for(tmp_path, AlwaysSpeech()), finals=1)

    assert _nothing_recognised_lines(stats, resolve_language("en")) == []


# ---------------------------------------------------------------------------------------
# A caller that only hears about events hears nothing from a recogniser producing none
# (ADR 0045)
#
# The window drove its input-quality warnings from the event loop. A run that produces no
# events produces no iterations of that loop, so a wrong-language run went by with the window
# saying "listening" and not even its level warnings updating.
# ---------------------------------------------------------------------------------------


def test_a_run_reports_every_frame_even_when_nothing_is_recognised(tmp_path: Path) -> None:
    calls: list[float] = []
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        SilentRecognizer(),
        detector=AlwaysSpeech(),  # type: ignore[arg-type]
        on_frame=lambda: calls.append(run.speech_seconds),
    )

    events = list(run.events())

    assert events == [], "the recogniser under test is meant to produce nothing"
    assert calls, "a run that produced no events reported nothing to its caller"
    assert len(calls) == run.stats.frames_read if run.stats else False
    assert calls == sorted(calls), "the speech count must only ever go up"
    assert calls[-1] == pytest.approx(run.speech_seconds)


def test_the_counts_are_readable_while_the_run_is_in_flight(tmp_path: Path) -> None:
    """The window asks between frames. Waiting for `stats` means waiting for the end of a
    live capture, which is when the user has already given up."""
    seen: list[tuple[float, int]] = []
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        SilentRecognizer(),
        detector=AlwaysSpeech(),  # type: ignore[arg-type]
        on_frame=lambda: seen.append((run.speech_seconds, run.finals_so_far)),
    )

    list(run.events())

    assert seen[0][0] > 0.0, "no speech had been counted after the first frame"
    assert all(finals == 0 for _, finals in seen)


def test_a_second_run_of_the_same_object_starts_its_counts_again(tmp_path: Path) -> None:
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        SilentRecognizer(),
        detector=AlwaysSpeech(),  # type: ignore[arg-type]
    )
    list(run.events())
    first = run.speech_seconds

    run._source = WavFileSource(speech_wav(tmp_path / "b.wav"))
    list(run.events())

    assert run.speech_seconds == pytest.approx(first), "the second run added to the first"


# ---------------------------------------------------------------------------------------
# The counts a run reports about itself
#
# `events 45 partial, 3 final` is printed by every run and quoted in the budget document,
# and a mutation sweep found the counters untested: incrementing by two, or starting at one,
# changed nothing that failed.
# ---------------------------------------------------------------------------------------


class ScriptedRecognizer:
    """Emits exactly the events it is given, one per frame, then whatever `finish` holds."""

    def __init__(
        self, script: list[TranscriptEvent], tail: list[TranscriptEvent] | None = None
    ) -> None:
        self._script = list(script)
        self._tail = list(tail or [])
        self.reset_calls = 0

    def emits_partials(self) -> bool:
        return True

    def accept(self, frame: bytes) -> tuple[TranscriptEvent, ...]:
        return (self._script.pop(0),) if self._script else ()

    def finish(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._tail)

    def reset(self) -> None:
        self.reset_calls += 1

    def validate_format(self, audio_format: AudioFormat) -> None: ...

    def warm_up(self) -> None: ...


def confident(text: str, *, is_final: bool, confidence: float | None = None) -> TranscriptEvent:
    return TranscriptEvent(
        utterance_index=1,
        text=text,
        is_final=is_final,
        audio_offset_seconds=0.0,
        latency_seconds=0.0,
        confidence=confidence,
    )


def run_over(tmp_path: Path, script: list[TranscriptEvent]) -> StreamingStats:
    run = StreamingRun(WavFileSource(speech_wav(tmp_path / "a.wav")), ScriptedRecognizer(script))
    list(run.events())
    stats = run.stats
    assert stats is not None
    return stats


def test_partials_and_finals_are_counted_exactly(tmp_path: Path) -> None:
    stats = run_over(
        tmp_path,
        [
            confident("one", is_final=False),
            confident("one two", is_final=False),
            confident("one two three", is_final=True),
            confident("four", is_final=True),
        ],
    )

    assert (stats.partials, stats.finals) == (2, 2)


def test_a_confidence_is_collected_only_when_the_recogniser_reported_one(
    tmp_path: Path,
) -> None:
    """`None` means the recogniser said nothing about its answer, and a run that counted it
    as a number would report a median of something nobody measured (ADR 0043)."""
    stats = run_over(
        tmp_path,
        [
            confident("a", is_final=True, confidence=-0.20),
            confident("b", is_final=True),
            confident("c", is_final=True, confidence=-0.40),
        ],
    )

    assert stats.confidences == (-0.20, -0.40)
    assert stats.median_confidence == pytest.approx(-0.30)


def test_a_run_with_no_confidences_has_no_median(tmp_path: Path) -> None:
    stats = run_over(tmp_path, [confident("a", is_final=True)])

    assert stats.confidences == ()
    assert stats.median_confidence is None


def test_a_second_run_counts_only_itself(tmp_path: Path) -> None:
    """The counters live on the run object between `events()` calls, and a run that added to
    the last one would report a conversation nobody had."""
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        ScriptedRecognizer([confident("a", is_final=True, confidence=-0.2)]),
    )
    list(run.events())

    run._source = WavFileSource(speech_wav(tmp_path / "b.wav"))
    run._recognizer = ScriptedRecognizer([confident("b", is_final=True, confidence=-0.3)])
    list(run.events())

    assert run.stats is not None
    assert run.stats.finals == 1
    assert run.stats.confidences == (-0.3,)
    assert run.finals_so_far == 1


def test_what_the_flush_tail_produces_is_counted_too(tmp_path: Path) -> None:
    """The real recogniser emits its last utterance from `finish`, not from a frame: a
    transducer cannot emit a symbol it has no future frames for (ADR 0023). Those events go
    through a second copy of the counting, and only the first copy was tested.
    """
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        ScriptedRecognizer(
            [confident("during", is_final=False)],
            tail=[confident("at the end", is_final=True, confidence=-0.5)],
        ),
    )

    texts = [event.text for event in run.events()]

    assert run.stats is not None
    assert texts == ["during", "at the end"]
    assert (run.stats.partials, run.stats.finals) == (1, 1)
    assert run.stats.confidences == (-0.5,)


def test_a_second_run_that_recognises_nothing_says_nothing_was_recognised(
    tmp_path: Path,
) -> None:
    """The live counter has to be cleared, not merely overwritten.

    `finals_so_far` is what the window's "speech went in and nothing came out" finding reads
    (ADR 0045). A run that kept the previous run's count would never report it — the second
    run would look like it had already recognised something.
    """
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        ScriptedRecognizer([confident("something", is_final=True)]),
    )
    list(run.events())
    assert run.finals_so_far == 1

    run._source = WavFileSource(speech_wav(tmp_path / "b.wav"))
    run._recognizer = ScriptedRecognizer([])
    list(run.events())

    assert run.finals_so_far == 0, "the second run inherited the first one's finals"


def test_a_run_that_has_not_started_has_recognised_nothing(tmp_path: Path) -> None:
    """The window constructs a run and reads this before the first frame (ADR 0045), so the
    initial value is a state somebody observes rather than a formality. A mutation setting
    it to one survived the whole suite."""
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        ScriptedRecognizer([confident("later", is_final=True)]),
    )

    assert run.finals_so_far == 0
    assert run.speech_seconds == 0.0
    assert run.stats is None


def test_a_partial_from_the_flush_tail_is_counted_as_one(tmp_path: Path) -> None:
    """The port says `finish` finalises or drops what was in flight, so this should not
    happen — and the counting handles it anyway, because the event is still yielded to the
    caller and an event that reaches a reader uncounted makes the summary a lie. Nothing
    exercised that branch: `partials += 1` in the tail loop could count two and no test in
    the suite noticed.
    """
    run = StreamingRun(
        WavFileSource(speech_wav(tmp_path / "a.wav")),
        ScriptedRecognizer([], tail=[confident("still talking", is_final=False)]),
    )

    texts = [event.text for event in run.events()]

    assert texts == ["still talking"]
    assert run.stats is not None
    assert (run.stats.partials, run.stats.finals) == (1, 0)


# --------------------------------------------------------------------------------------
# A conversation from a file (ADR 0047)
# --------------------------------------------------------------------------------------


class Confident:
    """A recogniser that finalises once, with a confidence it was told to have."""

    def __init__(self, text: str, confidence: float | None, *, at: int = 20) -> None:
        self._text = text
        self._confidence = confidence
        self._at = at
        self.frames = 0
        self.warmed = False
        self.validated = 0

    def warm_up(self) -> None:
        self.warmed = True

    def validate_format(self, audio_format: AudioFormat) -> None:
        self.validated += 1

    def reset(self) -> None:
        return None

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        self.frames += 1
        if self.frames != self._at:
            return ()
        return (
            TranscriptEvent(
                utterance_index=1,
                text=self._text,
                is_final=True,
                audio_offset_seconds=0.0,
                latency_seconds=0.0,
                confidence=self._confidence,
            ),
        )

    def finish(self) -> Sequence[TranscriptEvent]:
        return ()


def patch_conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Confident]:
    """Two recognisers told apart by the directory the model store hands them.

    Not by construction order: `_load_conversation` builds them on threads, and the order
    they are asked for is not the order they are built in.
    """
    built = {
        "en": Confident("hello there", -0.30),
        "fr": Confident("allo la la", -1.20),
    }

    def ensure(self: object, pin: object) -> Path:
        directory = tmp_path / str(getattr(pin, "name", pin))
        directory.mkdir(exist_ok=True)
        return directory

    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", ensure)
    monkeypatch.setattr(
        "on_the_fly.app.cli.SherpaStreamingRecognizer",
        lambda directory, **kwargs: built[Path(directory).name.removeprefix("streaming-")],
    )
    monkeypatch.setattr("on_the_fly.app.cli.open_translator", lambda *a, **k: SayingWhichWay())
    return built


class SayingWhichWay:
    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        return f"{text} in {target_language}"


def test_a_file_can_be_a_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run ADR 0047 records was taken from files, because a demonstration is not a
    reason to open somebody's microphone. The shipped command has to be able to reproduce
    it."""
    path = speech_wav(tmp_path / "a.wav")
    patch_conversation(monkeypatch, tmp_path)

    exit_code = main(["stream", str(path), "--conversation", "en:fr", "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "English (en, streaming), French (fr, streaming)" in output
    assert "[en] " in output, "the winning language is not named"
    assert "hello there" in output
    assert "allo la la" not in output, "the losing model's text was printed"
    assert "[fr] hello there in fr" in output
    assert "speakers      en 1, fr 0 final(s)" in output


def test_both_models_check_the_file_before_anything_is_streamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file one of the two cannot read is refused by that one, not by the first to be
    asked on a thread."""
    path = speech_wav(tmp_path / "a.wav")
    built = patch_conversation(monkeypatch, tmp_path)

    main(["stream", str(path), "--conversation", "en:fr", "--cache-dir", str(tmp_path)])

    assert all(recogniser.validated for recogniser in built.values())
    assert all(recogniser.warmed for recogniser in built.values())


def test_a_file_conversation_cannot_also_name_one_language(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["stream", "x.wav", "--conversation", "en:fr", "--language", "ru"])

    assert raised.value.code == 2
