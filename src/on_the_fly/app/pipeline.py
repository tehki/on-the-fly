"""The composition root: where the concrete pieces are wired together.

Handbook 4 and 41 ask for one visible place that knows about concrete implementations, so
that the domain does not. This is it. `EphemeralStore`, `ThreadedReaper`,
`EnergyVoiceActivityDetector` and `CaptureSession` are constructed here and injected;
nothing below this module chooses its own dependencies.

Keeping the security-sensitive dependencies visible here is the point. Reading this file
tells you that every run gets a retention store, that a reaper is started to enforce it,
and that the store is purged on the way out — none of which is discoverable by reading the
pipeline, and all of which would be easy to quietly omit if construction were scattered.

Everything this returns is metadata. Utterance audio stays in the store and expires there.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from dataclasses import dataclass

from on_the_fly.domain.audio import (
    AudioSource,
    CaptureSession,
    CaptureStats,
    EndReason,
    EnergyVoiceActivityDetector,
    SegmenterConfig,
    SpeechRecognizer,
    StreamingRecognizer,
    TranscriptEvent,
    VoiceActivityDetector,
)
from on_the_fly.domain.retention import (
    EphemeralStore,
    ReapReport,
    ThreadedReaper,
    TransientHandle,
    TransientRetentionPolicy,
)

DEFAULT_PROJECT_ID = "on-the-fly"


@dataclass(frozen=True)
class UtteranceRecord:
    """What a completed utterance looked like. Metadata only — never the audio.

    `OPERATIONAL_METADATA` in shape: a duration and a reason are safe to keep and print,
    a transcript would not be.
    """

    index: int
    start_seconds: float
    duration_seconds: float
    frame_count: int
    ended_because: EndReason
    # A handle is an identifier, not content: the transcript itself stays in the store,
    # under the same ten-second rule as the audio it came from.
    transcript_handle: TransientHandle | None = None
    recognition_seconds: float | None = None

    def __str__(self) -> str:
        return (
            f"#{self.index:<3} start={self.start_seconds:7.2f}s "
            f"duration={self.duration_seconds:5.2f}s "
            f"frames={self.frame_count:<5} ended={self.ended_because}"
        )


@dataclass(frozen=True)
class PipelineResult:
    """The outcome of one run. Safe to log, print, or serialise in full."""

    utterances: tuple[UtteranceRecord, ...]
    capture: CaptureStats
    wall_seconds: float
    final_reap: ReapReport
    entries_remaining: int
    # Present so a caller that asked to keep the store can read transcripts out and then
    # purge. None of this object's other fields carry content.
    store: EphemeralStore | None = None

    @property
    def audio_seconds(self) -> float:
        return self.capture.audio_seconds_seen

    @property
    def real_time_factor(self) -> float:
        """Wall time divided by audio duration.

        Below 1.0 means the pipeline keeps up with speech. This is the first number this
        project can measure rather than assume, and it covers segmentation only —
        recognition and translation are not implemented, so it is a floor, not a forecast.
        """
        if self.audio_seconds <= 0:
            return 0.0
        return self.wall_seconds / self.audio_seconds

    @property
    def retention_clean(self) -> bool:
        """True when the run ended with nothing retained and nothing failing to delete."""
        return self.entries_remaining == 0 and self.final_reap.ok


def build_store(
    project_id: str = DEFAULT_PROJECT_ID,
    *,
    retention_seconds: float | None = None,
) -> EphemeralStore:
    """Construct the retention store for a run.

    A non-default window is deliberately awkward to reach: `TransientRetentionPolicy`
    refuses anything above ten seconds without an `RetentionOverride`, so this cannot be
    used to quietly extend retention.
    """
    policy = (
        TransientRetentionPolicy(seconds=retention_seconds)
        if retention_seconds is not None
        else TransientRetentionPolicy.default()
    )
    return EphemeralStore(project_id, policy=policy)


def run_capture(
    source: AudioSource,
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    detector: VoiceActivityDetector | None = None,
    config: SegmenterConfig | None = None,
    store: EphemeralStore | None = None,
    retention_seconds: float | None = None,
    recognizer: SpeechRecognizer | None = None,
    keep_store: bool = False,
) -> PipelineResult:
    """Run one capture session to completion and report metadata about it.

    With a `recognizer`, each utterance is transcribed and the text is placed in the same
    store as the audio, under the same deadline. The result carries handles, not text: a
    caller that wants the words borrows them deliberately.

    The store is purged before returning unless `keep_store` is set. A run that ends
    leaving audio in memory has retained content past the point anyone needed it, and the
    caller of this function is not the right place to remember that. `keep_store` exists
    for the one legitimate case — a caller that must read the transcripts out first — and
    it makes that caller responsible for the purge.
    """
    active_store = (
        store if store is not None else build_store(project_id, retention_seconds=retention_seconds)
    )
    active_detector = detector if detector is not None else EnergyVoiceActivityDetector()

    session = CaptureSession(
        source=source, detector=active_detector, store=active_store, config=config
    )

    records: list[UtteranceRecord] = []
    elapsed_audio = 0.0
    started = time.monotonic()

    # The reaper is started for the run rather than left implicit. Even over a short file
    # this is the production wiring: expiry is clock-driven and does not wait to be asked.
    with ThreadedReaper(active_store), session:
        for index, utterance in enumerate(session.utterances(), start=1):
            transcript_handle: TransientHandle | None = None
            recognition_seconds: float | None = None

            if recognizer is not None:
                # Borrowing holds the audio against deletion for exactly as long as the
                # recogniser needs it, and restarts its window afterwards.
                recognition_started = time.monotonic()
                with active_store.borrow(utterance.handle) as audio:
                    if not isinstance(audio, bytes):
                        # The store can hold text as well as audio. Reaching a recogniser
                        # with the wrong one means a handle was crossed somewhere, which
                        # is a defect rather than something to coerce past.
                        raise TypeError(
                            f"utterance {utterance.handle.entry_id} holds "
                            f"{type(audio).__name__}, not audio"
                        )
                    text = recognizer.transcribe(audio, utterance.audio_format)
                recognition_seconds = time.monotonic() - recognition_started
                if text:
                    transcript_handle = active_store.put(
                        text, label="speech_recognition_transcript"
                    )

            records.append(
                UtteranceRecord(
                    index=index,
                    start_seconds=elapsed_audio,
                    duration_seconds=utterance.duration_seconds,
                    frame_count=utterance.frame_count,
                    ended_because=utterance.ended_because,
                    transcript_handle=transcript_handle,
                    recognition_seconds=recognition_seconds,
                )
            )
            elapsed_audio = session.stats.audio_seconds_seen

    wall_seconds = time.monotonic() - started

    # Shutdown deletes everything regardless of deadline (handbook 35). The report is kept
    # so a failed deletion is visible in the result rather than swallowed here.
    final_reap = ReapReport() if keep_store else active_store.purge_all()

    return PipelineResult(
        utterances=tuple(records),
        capture=session.stats,
        wall_seconds=wall_seconds,
        final_reap=final_reap,
        entries_remaining=len(active_store),
        store=active_store,
    )


@dataclass(frozen=True)
class StreamingStats:
    """What a streaming run did. Metadata only — the text went to the caller live."""

    frames_read: int
    audio_seconds: float
    wall_seconds: float
    partials: int
    finals: int
    first_text_after_seconds: float | None
    final_reap: ReapReport
    entries_remaining: int

    @property
    def real_time_factor(self) -> float:
        if self.audio_seconds <= 0:
            return 0.0
        return self.wall_seconds / self.audio_seconds

    @property
    def keeps_up(self) -> bool:
        """True when the run processed audio at least as fast as it arrived."""
        return self.real_time_factor < 1.0

    @property
    def retention_clean(self) -> bool:
        return self.entries_remaining == 0 and self.final_reap.ok


class StreamingRun:
    """Drives a streaming recogniser over a source, yielding results as they appear.

    The batch path returns everything at the end because it has nothing to say until then.
    This yields, because the entire point of a streaming recogniser is that text exists
    before the speaker has finished — a caller that waited for a return value would throw
    that away.

    Retention: finals are placed in the store as they are produced, so the transcript is
    accounted for and deleted on the usual clock rather than living as an untracked local
    variable. Partials are **not** stored — each is superseded within milliseconds, and
    keeping a trail of half-sentences would retain more content than the finished text does.

    Audio does not enter the store on this path at all. A transducer holds its own bounded
    internal buffers and does its own endpointing, so there is no assembled utterance to
    store; the bound the pre-roll ring provides on the batch path is the model's fixed
    chunk and left-context configuration here (ADR 0008).
    """

    def __init__(
        self,
        source: AudioSource,
        recognizer: StreamingRecognizer,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        store: EphemeralStore | None = None,
    ) -> None:
        self._source = source
        self._recognizer = recognizer
        self._store = store if store is not None else build_store(project_id)
        self._format = source.audio_format
        self._final_handles: list[TransientHandle] = []
        self._stats: StreamingStats | None = None

    @property
    def store(self) -> EphemeralStore:
        return self._store

    @property
    def final_handles(self) -> tuple[TransientHandle, ...]:
        """Handles to the finalised transcripts, in order. Identifiers, not content."""
        return tuple(self._final_handles)

    @property
    def stats(self) -> StreamingStats | None:
        """Available once the event stream has been consumed to completion."""
        return self._stats

    def events(self) -> Generator[TranscriptEvent, None, None]:
        """Yield transcript events as the audio is consumed.

        The source is closed and the store purged on every exit path, including a caller
        that stops consuming partway through.
        """
        frames = 0
        audio_seconds = 0.0
        partials = 0
        finals = 0
        first_text_after: float | None = None
        started = time.monotonic()

        try:
            for frame in self._source.frames():
                frames += 1
                audio_seconds += self._format.duration_seconds(len(frame))

                for event in self._recognizer.accept(frame):
                    if first_text_after is None:
                        first_text_after = audio_seconds
                    if event.is_final:
                        finals += 1
                        self._final_handles.append(
                            self._store.put(event.text, label="speech_recognition_transcript")
                        )
                    else:
                        partials += 1
                    yield event

            for event in self._recognizer.finish():
                if event.is_final:
                    finals += 1
                    self._final_handles.append(
                        self._store.put(event.text, label="speech_recognition_transcript")
                    )
                else:
                    partials += 1
                yield event
        finally:
            wall = time.monotonic() - started
            self._source.close()
            reap = self._store.purge_all()
            self._stats = StreamingStats(
                frames_read=frames,
                audio_seconds=audio_seconds,
                wall_seconds=wall,
                partials=partials,
                finals=finals,
                first_text_after_seconds=first_text_after,
                final_reap=reap,
                entries_remaining=len(self._store),
            )
