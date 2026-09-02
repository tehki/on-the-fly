"""Streaming recognition: results while someone is still speaking.

`SpeechRecognizer` takes a finished utterance and returns its text. That shape is what
made the measured latency inevitable — nothing can be shown until the speaker stops, and
then a whole inference runs before anything appears.

A streaming recogniser is a different shape. It consumes frames as they arrive and emits
events: **partial** results that may still change, and **final** results that will not.
That is what lets a caption appear while a sentence is still being spoken.

```text
frames ──► StreamingRecognizer ──► TranscriptEvent(partial)   "so I was think"
                                   TranscriptEvent(partial)   "so I was thinking we"
                                   TranscriptEvent(final)     "so I was thinking we should go"
```

The port is defined here, in the domain, with no dependency on any engine. Two things can
implement it:

* a genuine streaming model, which does its own endpointing and emits partials;
* `BatchStreamingRecognizer`, which drives the existing segmenter and batch recogniser and
  emits **finals only**.

The second one exists so the architecture is real today rather than aspirational. It does
not make Whisper fast — it cannot, the cost is in the model — but it means the pipeline,
the tests and the caller are already written against the interface a real streaming engine
needs, and swapping one in changes an adapter rather than the application.

Events carry text. That text is `EPHEMERAL` project content from the moment it exists; this
module hands it to its caller and writes it nowhere.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.ports import SpeechRecognizer
from on_the_fly.domain.audio.segmenter import Utterance, UtteranceSegmenter
from on_the_fly.domain.retention import EphemeralStore


@dataclass(frozen=True)
class TranscriptEvent:
    """One recognition result, partial or final.

    A partial may be replaced by a later event for the same utterance. A final will not
    change, and is the one a translator should act on — translating a partial wastes work
    and produces text that visibly rewrites itself.
    """

    utterance_index: int
    text: str
    is_final: bool
    audio_offset_seconds: float
    latency_seconds: float

    def __str__(self) -> str:
        # The text is the whole point of this object, so it appears. Callers that log
        # events rather than displaying them are logging project content, which
        # Article 14 does not permit.
        kind = "final  " if self.is_final else "partial"
        return f"[{self.audio_offset_seconds:7.2f}s {kind}] {self.text}"


class StreamingRecognizer(Protocol):
    """Consumes audio frames and emits results as they become available.

    Implementations may emit nothing for many frames, several events for one frame, or
    partials that are later superseded. Callers must treat the event stream as the truth
    rather than assuming one event per utterance.
    """

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        """Feed one frame. Returns any events that became available because of it."""
        ...

    def finish(self) -> Sequence[TranscriptEvent]:
        """Signal end of audio and return any remaining events.

        A partial that was in flight is either finalised or dropped; nothing is left
        pending, because a caller that has stopped sending audio will never call again.
        """
        ...

    def reset(self) -> None:
        """Discard all in-flight state, e.g. when the input device changes."""
        ...


class BatchStreamingRecognizer:
    """Presents a batch recogniser through the streaming interface. Finals only.

    Drives `UtteranceSegmenter`, and when an utterance completes, transcribes it and emits
    a single final event. There are no partials: a batch model has nothing to say until it
    has the whole utterance, and inventing partials by re-running it on prefixes would cost
    a full inference per partial for text that is likely to change.

    Honest about what it is. This makes the current recogniser usable through the streaming
    interface; it does not make it low-latency. `docs/PERFORMANCE_BUDGET.md` records what it
    actually costs.
    """

    def __init__(
        self,
        store: EphemeralStore,
        segmenter: UtteranceSegmenter,
        recognizer: SpeechRecognizer,
        audio_format: AudioFormat,
    ) -> None:
        self._store = store
        self._segmenter = segmenter
        self._recognizer = recognizer
        self._format = audio_format
        self._utterances = 0
        self._audio_seconds = 0.0

    @property
    def utterances_seen(self) -> int:
        return self._utterances

    @property
    def emits_partials(self) -> bool:
        """False, and callers may rely on that.

        A caption renderer can skip its "this text may change" handling when the recogniser
        promises never to produce a partial.
        """
        return False

    def __repr__(self) -> str:
        return (
            f"BatchStreamingRecognizer(utterances={self._utterances}, "
            f"partials={self.emits_partials})"
        )

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        self._audio_seconds += self._format.duration_seconds(len(frame))
        utterance = self._segmenter.push(frame)
        if utterance is None:
            return ()
        return self._transcribe(utterance_offset=self._audio_seconds, utterance=utterance)

    def finish(self) -> Sequence[TranscriptEvent]:
        utterance = self._segmenter.flush()
        if utterance is None:
            return ()
        return self._transcribe(utterance_offset=self._audio_seconds, utterance=utterance)

    def reset(self) -> None:
        self._segmenter.reset()
        self._utterances = 0
        self._audio_seconds = 0.0

    def _transcribe(
        self, *, utterance_offset: float, utterance: Utterance
    ) -> Sequence[TranscriptEvent]:
        self._utterances += 1

        started = time.monotonic()
        with self._store.borrow(utterance.handle) as audio:
            if not isinstance(audio, bytes):
                raise TypeError(
                    f"utterance {utterance.handle.entry_id} holds {type(audio).__name__}, not audio"
                )
            text = self._recognizer.transcribe(audio, utterance.audio_format)
        latency = time.monotonic() - started

        if not text:
            # Nothing recognised is not an event. Emitting an empty final would make a
            # caption renderer clear the screen for no reason.
            return ()

        return (
            TranscriptEvent(
                utterance_index=self._utterances,
                text=text,
                is_final=True,
                # The utterance ended here, so this is where its caption belongs.
                audio_offset_seconds=max(0.0, utterance_offset - utterance.duration_seconds),
                latency_seconds=latency,
            ),
        )
