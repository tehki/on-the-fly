"""Tests for model integrity, the recogniser, and recognition through the pipeline.

Almost nothing here needs a real model. `ModelStore` is about digests and refusals, which
are testable with a few bytes on disk, and the pipeline is tested with a fake recogniser so
that segmentation, retention and transcription wiring can be asserted in milliseconds.

The tests that do need the 78 MB Whisper model skip when it is absent, so a clone with no
model cache still runs the full suite.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
import wave
from pathlib import Path

import pytest

from on_the_fly.app import run_capture
from on_the_fly.app.cli import main
from on_the_fly.domain.audio import AudioFormat
from on_the_fly.infrastructure.asr import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    FasterWhisperRecognizer,
    ModelIntegrityError,
    ModelNotPresentError,
    ModelPin,
    ModelStore,
    ModelStoreError,
    RecognitionError,
    compute_digests,
    file_digest,
    resolve,
)
from on_the_fly.infrastructure.audio import WavFileSource

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
    assert "tiny (local, verified)" in output


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
    assert payload["model"] == "tiny"
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
