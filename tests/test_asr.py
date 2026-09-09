"""Tests for model integrity, the recogniser, and recognition through the pipeline.

Almost nothing here needs a real model. `ModelStore` is about digests and refusals, which
are testable with a few bytes on disk, and the pipeline is tested with a fake recogniser so
that segmentation, retention and transcription wiring can be asserted in milliseconds.

The tests that do need the 78 MB Whisper model skip when it is absent, so a clone with no
model cache still runs the full suite.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import struct
import sys
import tempfile
import types
import wave
from pathlib import Path
from typing import ClassVar

import pytest

from on_the_fly.app import run_capture
from on_the_fly.app.cli import main
from on_the_fly.domain.audio import AudioFormat
from on_the_fly.domain.audio.ports import ConfidenceReporting
from on_the_fly.infrastructure.asr import (
    BASE,
    DEFAULT_MODEL,
    KNOWN_MODELS,
    SMALL,
    TINY,
    FasterWhisperRecognizer,
    RecognitionError,
    Transcription,
    batch_pins,
    resolve,
)
from on_the_fly.infrastructure.asr.models import streaming_pins
from on_the_fly.infrastructure.audio import WavFileSource
from on_the_fly.infrastructure.model_store import (
    ModelIntegrityError,
    ModelNotPresentError,
    ModelPin,
    ModelStore,
    ModelStoreError,
    compute_digests,
    file_digest,
)

RATE = 16_000


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def place_model(directory: Path, files: dict[str, bytes]) -> ModelPin:
    """Write a fake model to disk and return a pin that matches it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_bytes(content)
    return ModelPin(
        name="fake",
        repo_id="example/fake-model",
        revision="0" * 40,
        licence="MIT",
        digests={name: sha256_of(content) for name, content in files.items()},
    )


def speech_like_wav(path: Path) -> Path:
    samples: list[int] = []
    for seconds, amplitude in ((0.4, 0), (1.0, 9000), (0.8, 0)):
        count = int(RATE * seconds)
        if amplitude == 0:
            samples += [0] * count
        else:
            samples += [
                int(amplitude * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(count)
            ]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


class FakeRecognizer:
    """Returns a fixed transcript. Fast, deterministic, and needs no model."""

    def __init__(self, text: str = "hello there") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
        self.calls += 1
        assert isinstance(audio, bytes)
        return self.text


# ======================================================================================
# ModelStore: refusals
# ======================================================================================


def test_an_unpinned_model_is_refused(tmp_path: Path) -> None:
    """The whole point. A model nobody pinned cannot be verified, so it is not loaded."""
    unpinned = ModelPin(
        name="unpinned", repo_id="example/x", revision="a" * 40, licence="MIT", digests={}
    )
    store = ModelStore(tmp_path, allow_download=True)

    with pytest.raises(ModelIntegrityError, match="no file digests"):
        store.ensure(unpinned)


def test_a_tampered_file_is_refused_and_left_in_place(tmp_path: Path) -> None:
    """A mismatch is a possible supply-chain event, not a cache miss to paper over."""
    model_dir = tmp_path / "fake" / ("0" * 40)
    pin = place_model(model_dir, {"config.json": b"{}", "model.bin": b"real weights"})

    (model_dir / "model.bin").write_bytes(b"tampered weights")
    store = ModelStore(tmp_path, allow_download=False)

    with pytest.raises(ModelIntegrityError, match="failed verification"):
        store.ensure(pin)

    assert (model_dir / "model.bin").read_bytes() == b"tampered weights", (
        "the suspect file must be kept for inspection, not deleted or overwritten"
    )


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    model_dir = tmp_path / "fake" / ("0" * 40)
    pin = place_model(model_dir, {"config.json": b"{}", "model.bin": b"weights"})
    (model_dir / "model.bin").unlink()

    with pytest.raises(ModelNotPresentError):
        ModelStore(tmp_path, allow_download=False).ensure(pin)


def test_downloading_is_off_by_default(tmp_path: Path) -> None:
    """Reaching the network is a distinct capability from reading a local file."""
    pin = ModelPin(
        name="absent",
        repo_id="example/x",
        revision="b" * 40,
        licence="MIT",
        digests={"model.bin": sha256_of(b"anything")},
    )
    store = ModelStore(tmp_path)

    assert store.allow_download is False
    with pytest.raises(ModelNotPresentError, match="downloading is not enabled"):
        store.ensure(pin)


def test_a_pin_naming_a_path_outside_its_directory_is_refused(tmp_path: Path) -> None:
    model_dir = tmp_path / "fake" / ("0" * 40)
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    escaping = ModelPin(
        name="fake",
        repo_id="example/x",
        revision="0" * 40,
        licence="MIT",
        digests={"../../escape.bin": sha256_of(b"weights")},
    )

    with pytest.raises(ModelIntegrityError, match="outside its own directory"):
        ModelStore(tmp_path).verify(escaping, model_dir)


def test_a_verified_model_resolves_to_its_directory(tmp_path: Path) -> None:
    model_dir = tmp_path / "fake" / ("0" * 40)
    pin = place_model(model_dir, {"config.json": b"{}", "model.bin": b"weights"})

    resolved = ModelStore(tmp_path, allow_download=False).ensure(pin)

    assert resolved == model_dir.resolve()


def test_a_pin_is_keyed_by_revision(tmp_path: Path) -> None:
    """Two revisions never share a directory, so an upgrade cannot half-overwrite one."""
    store = ModelStore(tmp_path)
    first = ModelPin(
        name="m", repo_id="e/x", revision="a" * 40, licence="MIT", digests={"f": "0" * 64}
    )
    second = ModelPin(
        name="m", repo_id="e/x", revision="b" * 40, licence="MIT", digests={"f": "0" * 64}
    )

    assert store.local_path(first) != store.local_path(second)


def test_a_malformed_pin_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="missing 'repo_id'"):
        ModelPin(name="m", repo_id="  ", revision="a" * 40, licence="MIT")
    with pytest.raises(ValueError, match="too short"):
        ModelPin(name="m", repo_id="e/x", revision="abc", licence="MIT")
    with pytest.raises(ValueError, match="SHA-256"):
        ModelPin(name="m", repo_id="e/x", revision="a" * 40, licence="MIT", digests={"f": "xy"})


# ======================================================================================
# Digest helpers and the registry
# ======================================================================================


def test_digests_are_computed_over_the_real_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"alpha")
    (tmp_path / "b.bin").write_bytes(b"beta")

    computed = compute_digests(tmp_path, ["a.bin", "b.bin"])

    assert computed["a.bin"] == sha256_of(b"alpha")
    assert computed["b.bin"] == file_digest(tmp_path / "b.bin")


def test_computing_digests_outside_the_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "inside").mkdir()
    with pytest.raises(ModelStoreError, match="outside the model directory"):
        compute_digests(tmp_path / "inside", ["../escape.bin"])


def test_only_pinned_models_can_be_named() -> None:
    """An arbitrary repository name here would reach any weights on the internet."""
    assert resolve(DEFAULT_MODEL.name) is DEFAULT_MODEL
    assert DEFAULT_MODEL.name in KNOWN_MODELS

    with pytest.raises(KeyError, match="unknown model"):
        resolve("large-v3")


def test_the_shipped_pin_is_complete() -> None:
    assert DEFAULT_MODEL.is_pinned
    assert DEFAULT_MODEL.licence == "MIT"
    assert set(DEFAULT_MODEL.digests) == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }


# ======================================================================================
# Recogniser
# ======================================================================================


def test_a_mismatched_sample_rate_is_refused_not_resampled(tmp_path: Path) -> None:
    """Same refusal the WAV source makes, for the same reason."""
    recognizer = FasterWhisperRecognizer(tmp_path)

    with pytest.raises(RecognitionError, match="expects 16000 Hz"):
        recognizer.transcribe(b"\x00\x00", AudioFormat(sample_rate_hz=44_100))


def test_empty_audio_transcribes_to_nothing_without_loading_a_model(tmp_path: Path) -> None:
    recognizer = FasterWhisperRecognizer(tmp_path / "does-not-exist")

    assert recognizer.transcribe(b"", AudioFormat()) == ""


def test_a_missing_model_directory_is_reported_clearly(tmp_path: Path) -> None:
    recognizer = FasterWhisperRecognizer(tmp_path / "absent")

    with pytest.raises(RecognitionError, match="model directory does not exist"):
        recognizer.transcribe(b"\x00\x00", AudioFormat())


def test_recognizer_repr_carries_no_audio_or_text(tmp_path: Path) -> None:
    rendered = repr(FasterWhisperRecognizer(tmp_path / "tiny"))

    assert "tiny" in rendered
    assert "loaded=False" in rendered


# ======================================================================================
# Recognition through the pipeline
# ======================================================================================


def test_transcripts_are_stored_under_the_same_deadline_as_the_audio(tmp_path: Path) -> None:
    """A transcript is project content the moment it exists."""
    source = WavFileSource(speech_like_wav(tmp_path / "speech.wav"))
    recognizer = FakeRecognizer("the patient is stable")

    result = run_capture(source, recognizer=recognizer, keep_store=True)

    assert recognizer.calls == len(result.utterances) == 1
    record = result.utterances[0]
    assert record.transcript_handle is not None
    assert record.recognition_seconds is not None

    store = result.store
    assert store is not None
    with store.borrow(record.transcript_handle) as text:
        assert text == "the patient is stable"

    # keep_store hands the caller the purge, and it still works.
    assert store.purge_all().ok
    assert len(store) == 0


def test_without_a_recognizer_no_transcript_is_produced(tmp_path: Path) -> None:
    source = WavFileSource(speech_like_wav(tmp_path / "speech.wav"))

    result = run_capture(source)

    assert result.utterances
    assert all(record.transcript_handle is None for record in result.utterances)
    assert result.retention_clean


def test_an_empty_transcript_stores_nothing(tmp_path: Path) -> None:
    """Whisper returns nothing for non-speech; that must not create an empty entry."""
    source = WavFileSource(speech_like_wav(tmp_path / "speech.wav"))

    result = run_capture(source, recognizer=FakeRecognizer(""), keep_store=True)

    assert result.utterances
    assert all(record.transcript_handle is None for record in result.utterances)
    store = result.store
    assert store is not None
    store.purge_all()


def test_the_cli_transcribes_and_purges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like_wav(tmp_path / "speech.wav")
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()

    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: model_dir)
    monkeypatch.setattr(
        "on_the_fly.app.cli.FasterWhisperRecognizer",
        lambda *args, **kwargs: FakeRecognizer("good morning"),
    )

    exit_code = main(["transcribe", str(path), "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "good morning" in output
    assert f"{DEFAULT_MODEL.name} (local, verified)" in output


def test_the_cli_transcribe_json_includes_text_and_timings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like_wav(tmp_path / "speech.wav")
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: model_dir)
    monkeypatch.setattr(
        "on_the_fly.app.cli.FasterWhisperRecognizer",
        lambda *args, **kwargs: FakeRecognizer("bonjour"),
    )

    exit_code = main(["transcribe", str(path), "--cache-dir", str(tmp_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model"] == DEFAULT_MODEL.name
    assert payload["utterances"][0]["text"] == "bonjour"
    assert "recognition_seconds" in payload["utterances"][0]


def test_the_cli_reports_an_unverifiable_model_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like_wav(tmp_path / "speech.wav")

    exit_code = main(["transcribe", str(path), "--cache-dir", str(tmp_path / "empty")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: " in captured.err
    assert "Traceback" not in captured.err


# ======================================================================================
# The real model, when it is present
# ======================================================================================


def real_model_dir() -> Path | None:
    """The verified model directory, or None when it has not been downloaded."""
    for cache in (
        Path.home() / ".cache" / "on-the-fly" / "models",
        # Where this project's own docs suggest putting it, on whatever this platform
        # calls the temporary directory.
        Path(tempfile.gettempdir()) / "otf-models",
    ):
        store = ModelStore(cache, allow_download=False)
        try:
            return store.ensure(DEFAULT_MODEL)
        except ModelStoreError:
            continue
    return None


def test_the_real_model_verifies_and_transcribes() -> None:
    """Exercises the actual model when it is on the machine, and skips when it is not."""
    model_dir = real_model_dir()
    if model_dir is None:
        pytest.skip("pinned model is not present in any known cache")

    recognizer = FasterWhisperRecognizer(model_dir)
    # Silence: a correct recogniser returns little or nothing rather than inventing speech.
    text = recognizer.transcribe(b"\x00\x00" * RATE, AudioFormat())

    assert isinstance(text, str)


# ======================================================================================
# What the model is loaded with
#
# Three arguments to `WhisperModel` are decisions with comments explaining them, and
# mutation testing found none of them pinned. One is a network-access control.
# ======================================================================================


class RecordingWhisperModel:
    """Stands in for faster-whisper. Records how it was constructed and transcribes nothing."""

    constructed: ClassVar[dict[str, object]] = {}
    called: ClassVar[dict[str, object]] = {}

    def __init__(self, model_dir: str, **kwargs: object) -> None:
        RecordingWhisperModel.constructed = {"model_dir": model_dir, **kwargs}

    def transcribe(self, samples: object, **kwargs: object) -> tuple[list[object], object]:
        RecordingWhisperModel.called = dict(kwargs)
        return [], None


def loaded_recognizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: object
) -> FasterWhisperRecognizer:
    """A recogniser that has loaded a fake model over a directory that exists."""
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = RecordingWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    model_dir = tmp_path / "tiny"
    model_dir.mkdir(exist_ok=True)
    recognizer = FasterWhisperRecognizer(model_dir, **kwargs)  # type: ignore[arg-type]
    recognizer.transcribe(b"\x00\x00" * 160, AudioFormat())
    return recognizer


def test_the_model_is_loaded_from_disk_and_never_from_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control the comment beside it describes.

    The weights on disk were put there by `ModelStore`, which checked them against a pinned
    digest. A loader permitted to download would be a second path to the same thing with no
    such check — the model would arrive, transcription would work, and nothing would have
    verified anything.
    """
    loaded_recognizer(monkeypatch, tmp_path)

    assert RecordingWhisperModel.constructed["local_files_only"] is True


def test_the_model_is_not_asked_to_segment_the_audio_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Segmentation already happened upstream, under bounds this project can explain and a
    retention window it enforces. Whisper's own VAD would cut the audio again on rules
    nobody here chose, and the utterance boundaries the caller was given would stop
    describing what was transcribed."""
    loaded_recognizer(monkeypatch, tmp_path)

    assert RecordingWhisperModel.called["vad_filter"] is False


def test_decoding_is_greedy_unless_a_caller_asks_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A beam of one. ADR 0014's argument, in the place it takes effect: the default is the
    cheap one, and paying for a wider search is a decision a caller makes."""
    loaded_recognizer(monkeypatch, tmp_path)

    assert RecordingWhisperModel.called["beam_size"] == 1


def test_a_wider_beam_is_passed_through_when_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded_recognizer(monkeypatch, tmp_path, beam_size=5)

    assert RecordingWhisperModel.called["beam_size"] == 5


def test_a_beam_of_zero_is_refused(tmp_path: Path) -> None:
    """Not a cheaper search: no search at all."""
    with pytest.raises(ValueError, match="beam_size"):
        FasterWhisperRecognizer(tmp_path, beam_size=0)


def test_the_model_directory_is_the_one_it_was_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verified directory, and not a name faster-whisper would resolve for itself —
    which is what `local_files_only` stops it doing."""
    loaded_recognizer(monkeypatch, tmp_path)

    assert RecordingWhisperModel.constructed["model_dir"] == str(tmp_path / "tiny")


def test_a_pin_cannot_be_edited_after_it_is_declared() -> None:
    """A `ModelPin` is the whole of this project's model trust: the repository, the revision
    and a digest for every file. Anything able to rewrite one at runtime turns every check
    built on it into a check of whatever was written last. A mutation making the class
    mutable survived every test here.
    """
    pin = resolve("streaming-en")

    with pytest.raises(dataclasses.FrozenInstanceError):
        pin.revision = "0" * 40  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        pin.digests = {}  # type: ignore[misc]


def test_the_shortest_thing_that_can_be_a_revision_is_seven_characters() -> None:
    """Git's own abbreviation floor, and the boundary was untested in both directions: only
    a full 40-character revision was ever asserted, so the check could have been anywhere."""
    ModelPin(
        name="short-but-legal",
        repo_id="example/model",
        revision="a" * 7,
        licence="Apache-2.0",
        digests={"model.bin": "0" * 64},
    )

    with pytest.raises(ValueError, match="revision"):
        ModelPin(
            name="too-short",
            repo_id="example/model",
            revision="a" * 6,
            licence="Apache-2.0",
            digests={"model.bin": "0" * 64},
        )


# ---------------------------------------------------------------------------------------
# Which models the batch command may be asked for
#
# `--model` offered every pin in the registry. `transcribe --model streaming-en` was an
# accepted argument that verified a 73 MB model and then failed inside CTranslate2 with
# `Unable to open file 'model.bin'` — a picker promising something it cannot serve, which is
# the defect ADR 0034 removed from the window's language pickers and left standing here.
# ---------------------------------------------------------------------------------------


def test_the_batch_pins_and_the_streaming_pins_partition_the_registry() -> None:
    """Derived from one rule, so the two lists cannot drift apart by an edit to one."""
    batch = set(batch_pins())
    streaming = set(streaming_pins())

    assert batch | streaming == set(KNOWN_MODELS)
    assert batch & streaming == set()
    assert batch, "the batch tier has no model at all"


def test_no_streaming_model_is_offered_to_the_batch_recogniser() -> None:
    for name in batch_pins():
        assert not name.startswith("streaming-"), f"{name} cannot be loaded by faster-whisper"


def test_the_default_model_is_one_the_batch_recogniser_can_load() -> None:
    """It is what `transcribe` uses when nobody says otherwise."""
    assert DEFAULT_MODEL.name in batch_pins()


def test_the_command_line_offers_exactly_the_batch_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asserted through the parser rather than the registry, because the defect was in the
    wiring between them and not in either."""
    with pytest.raises(SystemExit):
        main(["transcribe", str(tmp_path / "x.wav"), "--model", "streaming-en"])

    assert "invalid choice" in capsys.readouterr().err


def test_the_three_whisper_sizes_are_the_same_model_family() -> None:
    """`tokenizer.json` and `vocabulary.txt` are identical across the three, byte for byte,
    which is what one family in three sizes looks like from outside — and is the only check
    available that these are the models they claim to be beyond the publisher's word."""
    shared = [pin.digests["tokenizer.json"] for pin in (TINY, BASE, SMALL)]
    vocabularies = [pin.digests["vocabulary.txt"] for pin in (TINY, BASE, SMALL)]

    assert len(set(shared)) == 1, "the three sizes do not share a tokeniser"
    assert len(set(vocabularies)) == 1, "the three sizes do not share a vocabulary"
    assert len({pin.digests["model.bin"] for pin in (TINY, BASE, SMALL)}) == 3


# ======================================================================================
# What the model thought of its own answer (ADR 0042)
#
# ADR 0021 said this project could not detect a recogniser inventing words, and nothing
# could. faster-whisper raises the decoding temperature only when its own quality checks
# reject the greedy result, so a segment above zero is the model reporting that it fell back.
# ======================================================================================


class FakeSegment:
    """One decoded segment, shaped like faster-whisper's."""

    def __init__(
        self,
        text: str = "hello",
        *,
        start: float = 0.0,
        end: float = 1.0,
        avg_logprob: float = -0.2,
        no_speech_prob: float = 0.01,
        temperature: float = 0.0,
    ) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.temperature = temperature


def whisper_returning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, segments: list[FakeSegment]
) -> FasterWhisperRecognizer:
    class Model:
        def __init__(self, model_dir: str, **kwargs: object) -> None: ...

        def transcribe(self, samples: object, **kwargs: object) -> tuple[list[FakeSegment], object]:
            return segments, None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    return FasterWhisperRecognizer(model_dir)


def test_a_greedy_decode_is_not_a_failed_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recognizer = whisper_returning(monkeypatch, tmp_path, [FakeSegment(temperature=0.0)])

    result = recognizer.transcribe_with_confidence(b"\x00\x00" * 160, AudioFormat())

    assert result.text == "hello"
    assert not result.is_failed_decode


def test_a_segment_the_model_resampled_is_a_failed_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """0.2 is the first fallback step, and the one the measured French clip lands on."""
    recognizer = whisper_returning(monkeypatch, tmp_path, [FakeSegment(temperature=0.2)])

    assert recognizer.transcribe_with_confidence(b"\x00\x00" * 160, AudioFormat()).is_failed_decode


def test_one_failed_segment_fails_the_utterance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """However confident the model was about the rest of it."""
    recognizer = whisper_returning(
        monkeypatch,
        tmp_path,
        [FakeSegment(temperature=0.0), FakeSegment(temperature=1.0), FakeSegment(temperature=0.0)],
    )

    assert recognizer.transcribe_with_confidence(b"\x00\x00" * 160, AudioFormat()).is_failed_decode


def test_confidence_is_weighted_by_how_much_of_the_utterance_a_segment_covers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unweighted mean lets a short confident fragment speak for a long uncertain one."""
    recognizer = whisper_returning(
        monkeypatch,
        tmp_path,
        [
            FakeSegment(start=0.0, end=9.0, avg_logprob=-1.0),
            FakeSegment(start=9.0, end=10.0, avg_logprob=0.0),
        ],
    )

    result = recognizer.transcribe_with_confidence(b"\x00\x00" * 160, AudioFormat())

    assert result.confidence == pytest.approx(-0.9)


def test_segments_of_no_duration_still_produce_a_confidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dividing by a total duration of zero would be an exception in the middle of a run."""
    recognizer = whisper_returning(
        monkeypatch,
        tmp_path,
        [FakeSegment(start=1.0, end=1.0, avg_logprob=-0.4), FakeSegment(start=2.0, end=2.0)],
    )

    assert recognizer.transcribe_with_confidence(
        b"\x00\x00" * 160, AudioFormat()
    ).confidence == pytest.approx(-0.3)


def test_nothing_decoded_reports_no_opinion_rather_than_confidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`None` is not zero. A recogniser that decoded nothing has said nothing about it, and a
    confidence of 0.0 would read as certainty."""
    recognizer = whisper_returning(monkeypatch, tmp_path, [])

    result = recognizer.transcribe_with_confidence(b"\x00\x00" * 160, AudioFormat())

    assert result.text == ""
    assert result.confidence is None
    assert result.temperature is None
    assert not result.is_failed_decode


def test_empty_audio_reports_no_opinion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recognizer = whisper_returning(monkeypatch, tmp_path, [FakeSegment()])

    result = recognizer.transcribe_with_confidence(b"", AudioFormat())

    assert result == Transcription(text="", confidence=None, no_speech=None, temperature=None)


def test_the_port_still_returns_a_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`SpeechRecognizer.transcribe` is unchanged: the extra reporting is an optional
    capability, so a caller that does not ask for it sees exactly what it saw before."""
    recognizer = whisper_returning(monkeypatch, tmp_path, [FakeSegment(text=" spaced ")])

    assert recognizer.transcribe(b"\x00\x00" * 160, AudioFormat()) == "spaced"


def test_a_recogniser_that_reports_confidence_is_recognised_as_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert isinstance(whisper_returning(monkeypatch, tmp_path, []), ConfidenceReporting)


def test_a_recogniser_that_does_not_is_not() -> None:
    """The streaming transducer reports nothing comparable, and asking it to would mean every
    implementation answering a question only one of them can."""
    assert not isinstance(FakeRecognizer(), ConfidenceReporting)


def test_the_command_line_says_when_the_model_gave_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The text is still printed. A user who reads the language is a better judge of it than
    a threshold — but "the model rejected its own answer" is a thing to be told, and until
    ADR 0042 this project could not tell anyone (ADR 0021)."""

    class Uncertain:
        def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
            return "a different sentence"

        def transcribe_with_confidence(
            self, audio: bytes, audio_format: AudioFormat
        ) -> Transcription:
            return Transcription(
                text="a different sentence", confidence=-1.02, no_speech=0.09, temperature=0.2
            )

    path = speech_like_wav(tmp_path / "speech.wav")
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: tmp_path)
    monkeypatch.setattr("on_the_fly.app.cli.FasterWhisperRecognizer", lambda *a, **k: Uncertain())

    assert main(["transcribe", str(path), "--cache-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "a different sentence" in output, "the text is withheld rather than flagged"
    assert "rejected its own first answer" in output
    assert "-1.02" in output


def test_a_confident_transcript_carries_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise the marker means nothing. Every clip this project measured that comes back
    correct is decoded at temperature 0."""

    class Confident:
        def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
            return "the right sentence"

        def transcribe_with_confidence(
            self, audio: bytes, audio_format: AudioFormat
        ) -> Transcription:
            return Transcription(
                text="the right sentence", confidence=-0.36, no_speech=0.01, temperature=0.0
            )

    path = speech_like_wav(tmp_path / "speech.wav")
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: tmp_path)
    monkeypatch.setattr("on_the_fly.app.cli.FasterWhisperRecognizer", lambda *a, **k: Confident())

    assert main(["transcribe", str(path), "--cache-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "the right sentence" in output
    assert "rejected" not in output


def test_the_json_reports_the_confidence_and_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Uncertain:
        def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str:
            return "text"

        def transcribe_with_confidence(
            self, audio: bytes, audio_format: AudioFormat
        ) -> Transcription:
            return Transcription(text="text", confidence=-1.0444, no_speech=0.1, temperature=1.0)

    path = speech_like_wav(tmp_path / "speech.wav")
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: tmp_path)
    monkeypatch.setattr("on_the_fly.app.cli.FasterWhisperRecognizer", lambda *a, **k: Uncertain())

    assert main(["transcribe", str(path), "--cache-dir", str(tmp_path), "--json"]) == 0

    utterance = json.loads(capsys.readouterr().out)["utterances"][0]
    assert utterance["confidence"] == -1.044
    assert utterance["failed_decode"] is True


def test_a_recogniser_with_no_opinion_is_not_reported_as_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`None` is not a complaint. Treating it as one would put a warning on every caption
    produced by a recogniser that reports nothing."""
    path = speech_like_wav(tmp_path / "speech.wav")
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: tmp_path)
    monkeypatch.setattr(
        "on_the_fly.app.cli.FasterWhisperRecognizer", lambda *a, **k: FakeRecognizer("plain")
    )

    assert main(["transcribe", str(path), "--cache-dir", str(tmp_path), "--json"]) == 0

    utterance = json.loads(capsys.readouterr().out)["utterances"][0]
    assert utterance["confidence"] is None
    assert utterance["failed_decode"] is False
