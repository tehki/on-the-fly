"""Tests for the sherpa-onnx streaming recogniser.

Everything that can be checked without the 73 MB model is checked without it: format
refusal, missing files, laziness, the partials promise. The tests that need the real model
skip when it is absent, so a clone with no model cache still runs the whole suite.
"""

from __future__ import annotations

import tempfile
import wave
from array import array
from pathlib import Path

import pytest

from on_the_fly.domain.audio import AudioFormat, EndReason, TranscriptEvent
from on_the_fly.infrastructure.asr import (
    SherpaStreamingRecognizer,
    StreamingRecognitionError,
    resolve,
)
from on_the_fly.infrastructure.asr.models import (
    STREAMING_LAYOUTS,
    layout_for,
    streaming_pins,
)
from on_the_fly.infrastructure.asr.sherpa_streaming import (
    FLUSH_TAIL_SECONDS,
    MAX_UTTERANCE_SECONDS,
    SILENCE_AFTER_SPEECH_SECONDS,
    SILENCE_BEFORE_ANY_SPEECH_SECONDS,
)
from on_the_fly.infrastructure.model_store import ModelStore, ModelStoreError

RATE = 16_000

# Asked of the registry rather than written out. A hand-written list is a fourth thing that
# has to be kept in step with the pins, and it silently stops covering the pin it was not
# updated for — which is the whole failure this section is about.
STREAMING_PIN_NAMES = sorted(streaming_pins())


def test_the_registry_is_what_these_tests_are_asking_about() -> None:
    """The derived list is not empty and holds what this project actually streams.

    Without this, a `streaming_pins()` that returned nothing would make every parametrised
    test below vacuously pass.
    """
    assert STREAMING_PIN_NAMES == ["streaming-en", "streaming-fr", "streaming-ru"]


@pytest.mark.parametrize("name", STREAMING_PIN_NAMES)
def test_every_streaming_pin_is_complete_and_permissively_licensed(name: str) -> None:
    """Three languages, three publishers, one rule: pinned, and licensed so this can ship."""
    pin = resolve(name)

    assert pin.is_pinned
    assert pin.licence == "Apache-2.0"
    assert len(pin.digests) == 4
    assert any(entry.endswith("tokens.txt") for entry in pin.digests)


@pytest.mark.parametrize("name", STREAMING_PIN_NAMES)
def test_every_streaming_pin_has_a_layout_naming_files_it_pins(name: str) -> None:
    """A layout that names a file the pin does not cover would load unverified weights.

    Each publisher names these files differently — after a training epoch, a chunk size, or
    an icefall recipe — which is why the layout exists at all, and why it has to be checked
    against the pin rather than trusted to match it.
    """
    pin = resolve(name)
    layout = layout_for(pin)

    for role in (layout.encoder, layout.decoder, layout.joiner, layout.tokens):
        assert role in pin.digests, f"{name} layout names {role!r}, which is not pinned"


def test_no_layout_is_recorded_for_a_pin_that_does_not_exist() -> None:
    """The other direction: an orphan layout is a rename nobody finished."""
    for name in STREAMING_LAYOUTS:
        assert name in streaming_pins(), f"{name} has a layout but is not a streaming pin"


def test_a_pin_without_a_layout_is_refused_with_something_to_act_on() -> None:
    """The failure this whole seam exists for, in the shape `resolve()` already refuses.

    `STREAMING_LAYOUTS` is a separate dict from the pins it keys, so a pin can be added —
    and a language moved to the streaming tier alongside it — with nothing to prompt a
    layout. Indexing the dict directly gave a bare `KeyError` at the moment the recogniser
    was built. The whisper pin stands in for such a pin here: it is real, it is in the
    registry, and it has no layout, which is correct for it and is exactly the shape of the
    mistake.
    """
    with pytest.raises(KeyError, match="no file layout is recorded"):
        layout_for(resolve("tiny"))


def test_it_promises_partials() -> None:
    """Unlike the batch adapter. A caption renderer must handle text being replaced."""
    assert SherpaStreamingRecognizer(Path("unused")).emits_partials is True


def test_a_mismatched_sample_rate_is_refused_not_resampled(tmp_path: Path) -> None:
    recognizer = SherpaStreamingRecognizer(tmp_path)

    with pytest.raises(StreamingRecognitionError, match="expects 16000 Hz"):
        recognizer.validate_format(AudioFormat(sample_rate_hz=44_100))

    recognizer.validate_format(AudioFormat())


def test_an_empty_frame_does_nothing_and_loads_nothing(tmp_path: Path) -> None:
    """Laziness matters: a session that never hears audio should not pay for a model."""
    recognizer = SherpaStreamingRecognizer(tmp_path / "absent")

    assert recognizer.accept(b"") == ()
    assert "loaded=False" in repr(recognizer)


def test_a_missing_model_directory_is_reported_clearly(tmp_path: Path) -> None:
    recognizer = SherpaStreamingRecognizer(tmp_path / "absent")

    with pytest.raises(StreamingRecognitionError, match="missing"):
        recognizer.accept(b"\x00\x00" * 320)


def test_an_incomplete_model_directory_names_what_is_missing(tmp_path: Path) -> None:
    """A directory with some of the files is more dangerous than an empty one."""
    (tmp_path / "tokens.txt").write_text("dummy", encoding="utf-8")
    recognizer = SherpaStreamingRecognizer(tmp_path)

    with pytest.raises(StreamingRecognitionError, match="encoder"):
        recognizer.accept(b"\x00\x00" * 320)


def test_thread_count_is_validated() -> None:
    with pytest.raises(ValueError, match="num_threads"):
        SherpaStreamingRecognizer(Path("unused"), num_threads=0)


def test_repr_carries_no_transcript(tmp_path: Path) -> None:
    rendered = repr(SherpaStreamingRecognizer(tmp_path / "streaming-en"))

    assert "streaming-en" in rendered
    assert "threads=" in rendered


# ======================================================================================
# The real model, when it is present
# ======================================================================================


def real_streaming_model() -> Path | None:
    for cache in (
        Path.home() / ".cache" / "on-the-fly" / "models",
        Path(tempfile.gettempdir()) / "otf-models",
    ):
        try:
            return ModelStore(cache, allow_download=False).ensure(resolve("streaming-en"))
        except ModelStoreError:
            continue
    return None


def published_speech_sample() -> Path | None:
    """A real speech WAV published alongside the model, if it was fetched."""
    candidate = Path(tempfile.gettempdir()) / "otf-testwav" / "test_wavs" / "0.wav"
    return candidate if candidate.is_file() else None


def unbroken_speech(audio: bytes, rate: int, *, copies: int = 3) -> bytes:
    """The sample with its trailing silence trimmed, repeated — speech with no pauses in it.

    The published clip carries 0.86 s of silence at its end, which is longer than
    `SILENCE_AFTER_SPEECH_SECONDS`, so simply repeating it produces pauses and tests the
    silence rule instead of the ceiling. Trimming makes it what it is meant to be here: a
    speaker who does not stop.
    """
    frame_bytes = (rate // 50) * 2
    end = len(audio)
    while end > frame_bytes:
        chunk = audio[end - frame_bytes : end]
        if max(abs(value) for value in array("h", chunk)) > 1200:
            break
        end -= frame_bytes
    return audio[:end] * copies


def test_silence_produces_no_events() -> None:
    model_dir = real_streaming_model()
    if model_dir is None:
        pytest.skip("pinned streaming model is not present in any known cache")

    recognizer = SherpaStreamingRecognizer(model_dir)
    events: list[TranscriptEvent] = []
    for _ in range(50):  # one second of silence
        events.extend(recognizer.accept(b"\x00\x00" * 320))

    assert events == [], "silence must not invent words"


def test_real_speech_produces_partials_then_a_final() -> None:
    """The behaviour the whole streaming decision rests on."""
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        assert reader.getframerate() == RATE
        audio = reader.readframes(reader.getnframes())

    recognizer = SherpaStreamingRecognizer(model_dir, num_threads=2)
    frame_bytes = 640
    events: list[TranscriptEvent] = []
    for offset in range(0, len(audio) - frame_bytes, frame_bytes):
        events.extend(recognizer.accept(audio[offset : offset + frame_bytes]))
    events.extend(recognizer.finish())

    partials = [e for e in events if not e.is_final]
    finals = [e for e in events if e.is_final]

    assert partials, "a streaming recogniser must emit partials"
    assert finals, "and must eventually finalise"
    # Partials grow: the model revises a hypothesis rather than appending blindly.
    assert len(partials[0].text) < len(partials[-1].text)
    assert finals[-1].text.strip()


# ======================================================================================
# Flushing. A transducer cannot emit a symbol it has no future frames for, so the last
# word of every stream used to be truncated or lost outright.
# ======================================================================================


def test_the_flush_tail_clears_the_largest_measured_requirement() -> None:
    """1000 ms is the longest tail any pinned export needs — the Russian one.

    The bound used to be `<= 1.0`, written when only English and French had been measured
    and 500 ms looked like 300 ms plus margin. Russian needed 1000 ms and lost the tail of
    every stream for a day. The lower bound here is now the measurement rather than a guess,
    so shortening this constant below what a pinned model actually needs fails.
    """
    largest_measured_requirement = 1.0

    assert FLUSH_TAIL_SECONDS >= largest_measured_requirement * 1.25, (
        "leave real margin: the last value chosen without it was wrong for the model that "
        "had not been measured"
    )
    # And still bounded. This is paid once per stream, but it is decoding time.
    assert FLUSH_TAIL_SECONDS <= 3.0


def test_finishing_a_recogniser_that_never_loaded_does_nothing() -> None:
    """The tail must not be the thing that forces a model load at the end of a silent run."""
    recognizer = SherpaStreamingRecognizer(Path("absent"))

    assert recognizer.finish() == ()
    assert "loaded=False" in repr(recognizer)


def test_finishing_twice_does_not_emit_the_utterance_again() -> None:
    """A closed stream cannot take another tail, and a second read would double-count."""
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())

    recognizer = SherpaStreamingRecognizer(model_dir)
    for offset in range(0, len(audio), 640):
        recognizer.accept(audio[offset : offset + 640])

    assert recognizer.finish(), "the first call must produce the trailing utterance"
    seen = recognizer.utterances_seen
    assert recognizer.finish() == ()
    assert recognizer.utterances_seen == seen


def test_resetting_reopens_a_finished_recogniser() -> None:
    """Otherwise a session that stopped could never start again without reloading."""
    model_dir = real_streaming_model()
    if model_dir is None:
        pytest.skip("pinned streaming model is not present in any known cache")

    recognizer = SherpaStreamingRecognizer(model_dir)
    recognizer.accept(b"\x00\x00" * 320)
    recognizer.finish()
    recognizer.reset()

    assert recognizer.accept(b"\x00\x00" * 320) == ()
    assert recognizer.finish() == ()


def test_the_last_word_survives_the_end_of_the_stream() -> None:
    """The regression. Without the flush tail this sample ends `...OF THE BROTHEL`.

    The word is named rather than inferred because the publisher ships the reference beside
    the audio: `AFTER EARLY NIGHTFALL ... THE SQUALID QUARTER OF THE BROTHELS`.
    """
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())

    recognizer = SherpaStreamingRecognizer(model_dir)
    events: list[TranscriptEvent] = []
    for offset in range(0, len(audio), 640):
        events.extend(recognizer.accept(audio[offset : offset + 640]))
    events.extend(recognizer.finish())

    spoken = " ".join(event.text for event in events if event.is_final)
    assert "BROTHELS" in spoken, f"the last word was lost or truncated: {spoken[-40:]!r}"


def test_the_flush_tail_does_not_invent_words() -> None:
    """Silence fed to a recogniser is how this project's worst failure mode starts.

    An amplified room produces confident nonsense (ADR 0021), so a tail of manufactured
    silence has to be shown not to decode to anything at all.
    """
    model_dir = real_streaming_model()
    if model_dir is None:
        pytest.skip("pinned streaming model is not present in any known cache")

    recognizer = SherpaStreamingRecognizer(model_dir)
    for _ in range(50):  # one second of silence, then the tail on top of it
        recognizer.accept(b"\x00\x00" * 320)

    assert recognizer.finish() == ()


def test_the_flush_tail_is_not_counted_as_audio_that_arrived() -> None:
    """It is silence this recogniser made up, so it must not appear in a duration.

    A caller reporting "7.13s of audio" after feeding a 7.13 s file would otherwise be
    reporting 7.63, and every real-time factor derived from it would be wrong.
    """
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())
        seconds = reader.getnframes() / reader.getframerate()

    recognizer = SherpaStreamingRecognizer(model_dir)
    events: list[TranscriptEvent] = []
    for offset in range(0, len(audio), 640):
        events.extend(recognizer.accept(audio[offset : offset + 640]))
    events.extend(recognizer.finish())

    finals = [event for event in events if event.is_final]
    covered = max(f.audio_offset_seconds + (f.duration_seconds or 0.0) for f in finals)
    assert covered <= seconds + 0.05, "the manufactured tail leaked into a reported duration"


# ======================================================================================
# Endpointing (ADR 0022)
# ======================================================================================


def test_the_endpoint_rules_are_seconds_and_plausibly_so() -> None:
    """The bug this guards against was a units error, and units errors are silent.

    A ceiling of 300 reads perfectly well as frames and is five minutes as seconds. Nothing
    fails, nothing logs, and utterances simply stop ending.
    """
    assert 0.0 < SILENCE_AFTER_SPEECH_SECONDS <= SILENCE_BEFORE_ANY_SPEECH_SECONDS
    # A conversation's turn, not a lecture. Anything above this is not a ceiling.
    assert SILENCE_BEFORE_ANY_SPEECH_SECONDS < MAX_UTTERANCE_SECONDS <= 15.0


def test_continuous_speech_is_broken_into_utterances() -> None:
    """Speech that never pauses must still produce more than one final.

    The regression: with the ceiling disabled, only trailing silence could end an utterance,
    so someone reading aloud produced a single final covering everything they said and a
    translation that arrived after they stopped.
    """
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())
        rate = reader.getframerate()

    # Trimmed and repeated, so there is no pause anywhere in it and only the ceiling can fire.
    continuous = unbroken_speech(audio, rate)
    seconds = len(continuous) / (rate * 2)
    assert seconds > MAX_UTTERANCE_SECONDS, "the sample must outlast the ceiling to test it"

    recognizer = SherpaStreamingRecognizer(model_dir)
    frame_bytes = (rate // 50) * 2
    finals: list[TranscriptEvent] = []
    for offset in range(0, len(continuous) - frame_bytes, frame_bytes):
        finals.extend(
            event
            for event in recognizer.accept(continuous[offset : offset + frame_bytes])
            if event.is_final
        )

    # The last utterance is still open when the audio runs out; the pipeline always calls
    # finish(), and without it the tail would be measured as an utterance that never ended.
    finals.extend(event for event in recognizer.finish() if event.is_final)

    assert len(finals) > 1, "continuous speech produced one run-on utterance"
    starts = [event.audio_offset_seconds for event in finals]
    longest = max(
        [starts[i + 1] - starts[i] for i in range(len(starts) - 1)] + [seconds - starts[-1]]
    )
    # The ceiling is a clock, so an utterance may overrun it by the frame it is detected in.
    assert longest <= MAX_UTTERANCE_SECONDS + 1.0, f"an utterance ran {longest:.1f}s"


def test_silence_produces_endpoints_that_are_counted_not_lost() -> None:
    """A long silence and an endpointer that never fires must not look the same.

    Before this counter, both produced the same thing from outside: a large gap between
    finals, and no way to tell which had happened (ADR 0023).
    """
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())
        rate = reader.getframerate()

    recognizer = SherpaStreamingRecognizer(model_dir)
    frame_bytes = (rate // 50) * 2
    speech_then_silence = audio + b"\x00\x00" * (rate * 12)
    finals: list[TranscriptEvent] = []
    for offset in range(0, len(speech_then_silence) - frame_bytes, frame_bytes):
        finals.extend(
            event
            for event in recognizer.accept(speech_then_silence[offset : offset + frame_bytes])
            if event.is_final
        )

    assert recognizer.silent_endpoints > 0, "twelve seconds of silence endpointed nothing"
    assert finals, "the speech before the silence still has to produce a final"
    for event in finals:
        assert event.shape, "a final must say how long it ran and what stopped it"
        assert event.duration_seconds is not None


def test_a_ceiling_ended_utterance_says_so() -> None:
    """The end reason is inferred, so it is worth checking against the rule it infers."""
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())
        rate = reader.getframerate()

    recognizer = SherpaStreamingRecognizer(model_dir)
    frame_bytes = (rate // 50) * 2
    continuous = unbroken_speech(audio, rate)
    finals: list[TranscriptEvent] = []
    for offset in range(0, len(continuous) - frame_bytes, frame_bytes):
        finals.extend(
            event
            for event in recognizer.accept(continuous[offset : offset + frame_bytes])
            if event.is_final
        )
    tail = [event for event in recognizer.finish() if event.is_final]

    assert finals, "continuous speech must be cut by the ceiling"
    for event in finals:
        assert event.end_reason is EndReason.MAX_DURATION
        assert event.duration_seconds is not None
        assert event.duration_seconds >= MAX_UTTERANCE_SECONDS - 0.02
    for event in tail:
        # Whatever is still open when the audio stops was ended by the audio stopping.
        assert event.end_reason is EndReason.FLUSH


def test_a_pause_ends_an_utterance_before_the_ceiling_does() -> None:
    """The point of tuning the silence rule (ADR 0024).

    The published clip carries 0.86 s of trailing silence. Repeated, that is a speaker who
    pauses between sentences, and the silence rule must be what cuts them — not the clock.
    At the publisher's 1.2 s it was the clock, and the cuts landed mid-word.
    """
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        audio = reader.readframes(reader.getnframes())
        rate = reader.getframerate()

    recognizer = SherpaStreamingRecognizer(model_dir)
    frame_bytes = (rate // 50) * 2
    with_pauses = audio * 3
    finals: list[TranscriptEvent] = []
    for offset in range(0, len(with_pauses) - frame_bytes, frame_bytes):
        finals.extend(
            event
            for event in recognizer.accept(with_pauses[offset : offset + frame_bytes])
            if event.is_final
        )

    assert finals, "three sentences with pauses between them must produce finals"
    assert all(event.end_reason is EndReason.SILENCE for event in finals), (
        "a speaker who pauses must be cut by the pause, not by the ceiling"
    )
    # Each repetition is a whole sentence, so no cut lands inside a word.
    assert all(event.duration_seconds is not None for event in finals)
    assert all(
        event.duration_seconds < MAX_UTTERANCE_SECONDS  # type: ignore[operator]
        for event in finals
    )


def test_the_silence_rule_is_short_enough_to_fire_on_conversation() -> None:
    """Calibrated against 75 measured pauses; at 1.2 s it fired on two of them (ADR 0024)."""
    assert SILENCE_AFTER_SPEECH_SECONDS <= 0.6, (
        "above this the rule stops firing on natural speech and the ceiling does the cutting"
    )
    # Still long enough that a gap between words is not a sentence boundary: the measured
    # within-speech cluster sits at 0.12-0.22 s.
    assert SILENCE_AFTER_SPEECH_SECONDS >= 0.3
