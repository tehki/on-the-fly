"""The interfaces this pipeline depends on, and nothing more.

Handbook 2 and ADR 0002 both require the core to be ignorant of what it is attached to.
These ports are that boundary: the domain knows there is something producing frames and
something detecting speech, and knows nothing about PortAudio, Qt, whisper.cpp, or a phone
microphone. Phase 2 replaces implementations behind these; it does not touch the pipeline.

`SpeechRecognizer` and `Translator` are declared here without implementations on purpose.
Naming the shape of a dependency is free; adopting a model is a dependency-admission
decision under Article 12 and is made separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from on_the_fly.domain.audio.formats import AudioFormat


@runtime_checkable
class AudioSource(Protocol):
    """Something producing PCM frames — a microphone, a file, a test fixture."""

    @property
    def audio_format(self) -> AudioFormat:
        """The format every frame from `frames()` is in."""
        ...

    def frames(self) -> Iterator[bytes]:
        """Yield PCM frames until the source ends or is closed.

        May block between frames. Implementations must yield whole samples; the pipeline
        validates this rather than trusting it.
        """
        ...

    def close(self) -> None:
        """Release the device. Safe to call more than once."""
        ...


@runtime_checkable
class VoiceActivityDetector(Protocol):
    """Decides whether a frame contains speech.

    Deliberately per-frame and stateless from the caller's perspective. An implementation
    may keep internal state such as a noise floor, but the segmenter owns all the timing
    decisions — hangover, minimum length, maximum length — so that changing the detector
    cannot silently change how utterances are cut.
    """

    def is_speech(self, frame: bytes) -> bool: ...

    def reset(self) -> None:
        """Discard adaptive state, e.g. when the input device changes."""
        ...


class SpeechRecognizer(Protocol):
    """Turns an utterance of audio into text. No implementation yet.

    The implementation will be a local model (ADR 0001). Whichever it is, it is admitted
    under Article 12 with a recorded source, licence, checksum and fail-closed behaviour
    on verification failure — model weights are executable trust like any dependency.
    """

    def transcribe(self, audio: bytes, audio_format: AudioFormat) -> str: ...


class Translator(Protocol):
    """Turns text in one language into another. No implementation yet.

    Same admission requirements as `SpeechRecognizer`. Note that several of the strongest
    multilingual models are non-commercial; see ADR 0001.
    """

    def translate(self, text: str, *, source_language: str, target_language: str) -> str: ...
