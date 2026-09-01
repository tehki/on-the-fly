"""Tests for audio capture, voice activity detection and utterance segmentation.

Segmentation is tested against a scripted detector rather than the energy one. The two
concerns are separate: whether a frame contains speech, and how frames are cut into
utterances. Testing them together would mean a change to the detector's sensitivity broke
segmentation tests, which is exactly the coupling `ports.py` exists to avoid.

The energy detector is tested on its own, against synthesised audio.
"""

from __future__ import annotations

from array import array
from collections.abc import Iterator

import pytest

from on_the_fly.domain.audio import (
    AudioFormat,
    CaptureError,
    CaptureSession,
    CaptureStats,
    EndReason,
    EnergyVoiceActivityDetector,
    SegmenterConfig,
    UtteranceSegmenter,
    frame_rms,
)
from on_the_fly.domain.retention import EphemeralStore, ManualClock

FORMAT = AudioFormat()
FRAME_MS = 20
FRAME_BYTES = FORMAT.frame_bytes(FRAME_MS)
SAMPLES_PER_FRAME = FRAME_BYTES // FORMAT.sample_width_bytes

TEST_CONFIG = SegmenterConfig(
    frame_ms=FRAME_MS,
    pre_roll_ms=60,  # 3 frames
    hangover_ms=40,  # 2 frames
    min_utterance_ms=20,  # 1 frame
    max_utterance_ms=1000,  # 50 frames
)


def frame_of(value: int) -> bytes:
    """A frame where every sample has the same amplitude, so audio is identifiable."""
    return array("h", [value] * SAMPLES_PER_FRAME).tobytes()


SILENT_FRAME = frame_of(0)
LOUD_FRAME = frame_of(8000)


class ScriptedDetector:
    """A detector that reads its answers from a script. Deterministic by construction."""

    def __init__(self, script: list[bool]) -> None:
        self.script = list(script)
        self.index = 0
        self.resets = 0

    def is_speech(self, frame: bytes) -> bool:
        if self.index >= len(self.script):
            return False
        answer = self.script[self.index]
        self.index += 1
        return answer

    def reset(self) -> None:
        self.resets += 1
        self.index = 0


class FakeSource:
    """An audio source that plays back a scripted list of frames."""

    def __init__(
        self,
        frames: list[bytes],
        *,
        audio_format: AudioFormat = FORMAT,
        fail_after: int | None = None,
    ) -> None:
        self._frames = frames
        self._format = audio_format
        self._fail_after = fail_after
        self.closed = 0

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def frames(self) -> Iterator[bytes]:
        for index, frame in enumerate(self._frames):
            if self._fail_after is not None and index >= self._fail_after:
                raise OSError("device disconnected")
            yield frame

    def close(self) -> None:
        self.closed += 1


def make_store(clock: ManualClock | None = None) -> EphemeralStore:
    return EphemeralStore("on-the-fly", clock=clock if clock is not None else ManualClock())


def make_segmenter(
    script: list[bool],
    store: EphemeralStore | None = None,
    config: SegmenterConfig | None = None,
) -> tuple[UtteranceSegmenter, EphemeralStore]:
    target = store if store is not None else make_store()
    segmenter = UtteranceSegmenter(
        store=target,
        detector=ScriptedDetector(script),
        audio_format=FORMAT,
        config=config if config is not None else TEST_CONFIG,
    )
    return segmenter, target


# ======================================================================================
# AudioFormat
# ======================================================================================


def test_audio_format_arithmetic() -> None:
    assert FORMAT.bytes_per_second == 16_000 * 2
    assert FORMAT.frame_bytes(20) == 640
    assert FORMAT.duration_seconds(32_000) == pytest.approx(1.0)


def test_audio_format_rejects_unsupported_shapes() -> None:
    with pytest.raises(ValueError, match="mono"):
        AudioFormat(channels=2)
    with pytest.raises(ValueError, match="16-bit"):
        AudioFormat(sample_width_bytes=4)
    with pytest.raises(ValueError, match="sample_rate_hz"):
        AudioFormat(sample_rate_hz=0)


def test_audio_format_rejects_a_frame_duration_that_is_not_whole_samples() -> None:
    """A partial sample shifts every later sample by a byte and turns speech into noise."""
    odd = AudioFormat(sample_rate_hz=44_100)
    with pytest.raises(ValueError, match="whole number of samples"):
        odd.frame_bytes(1)


def test_audio_format_rejects_a_truncated_frame() -> None:
    FORMAT.validate_frame(SILENT_FRAME)
    with pytest.raises(ValueError, match="whole number"):
        FORMAT.validate_frame(b"\x00\x01\x02")


# ======================================================================================
# Energy VAD
# ======================================================================================


def test_frame_rms_of_silence_and_tone() -> None:
    assert frame_rms(b"") == 0.0
    assert frame_rms(SILENT_FRAME) == pytest.approx(0.0)
    assert frame_rms(frame_of(8000)) == pytest.approx(8000.0)

    with pytest.raises(ValueError, match="whole number"):
        frame_rms(b"\x01")


def test_energy_detector_separates_silence_from_speech() -> None:
    detector = EnergyVoiceActivityDetector()
    assert detector.is_speech(SILENT_FRAME) is False
    assert detector.is_speech(LOUD_FRAME) is True


def test_energy_detector_adapts_to_room_tone_but_not_to_speech() -> None:
    """The noise floor must not creep up while someone is talking.

    Adapting on speech would raise the threshold until the speaker stopped being audible
    to the detector — quietly, and worse the longer they talked.
    """
    detector = EnergyVoiceActivityDetector()
    room_tone = frame_of(200)

    for _ in range(200):
        detector.is_speech(room_tone)
    settled_floor = detector.noise_floor
    assert settled_floor == pytest.approx(200.0, abs=5.0)

    for _ in range(200):
        assert detector.is_speech(LOUD_FRAME) is True
    assert detector.noise_floor == pytest.approx(settled_floor), (
        "the noise floor moved while speech was present"
    )

    detector.reset()
    assert detector.noise_floor == 0.0


def test_energy_detector_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError, match="speech_factor"):
        EnergyVoiceActivityDetector(speech_factor=1.0)
    with pytest.raises(ValueError, match="adaptation_rate"):
        EnergyVoiceActivityDetector(adaptation_rate=0.0)
    with pytest.raises(ValueError, match="adaptation_rate"):
        EnergyVoiceActivityDetector(adaptation_rate=1.0)


# ======================================================================================
# Segmentation
# ======================================================================================


def test_utterance_is_emitted_after_hangover_silence() -> None:
    # 5 silence, 3 speech, 2 silence: the second trailing silent frame reaches hangover.
    script = [False] * 5 + [True] * 3 + [False] * 2
    segmenter, store = make_segmenter(script)

    emitted = [segmenter.push(SILENT_FRAME if not s else LOUD_FRAME) for s in script]
    utterances = [u for u in emitted if u is not None]

    assert len(utterances) == 1
    utterance = utterances[0]
    assert utterance.ended_because is EndReason.SILENCE
    # 3 pre-roll frames + 3 speech + 2 trailing silence
    assert utterance.frame_count == 8
    assert utterance.duration_seconds == pytest.approx(8 * FRAME_MS / 1000)
    assert store.is_present(utterance.handle)


def test_pre_roll_is_included_so_the_first_syllable_is_not_clipped() -> None:
    """Speech is detected slightly after it starts; the ring recovers those frames."""
    script = [False] * 5 + [True] * 2 + [False] * 2
    segmenter, store = make_segmenter(script)

    marker = frame_of(1234)
    utterance = None
    for index, is_speech in enumerate(script):
        # Give the last three silent frames a distinctive amplitude so we can prove they
        # ended up at the head of the utterance.
        frame = marker if index in (2, 3, 4) else (LOUD_FRAME if is_speech else SILENT_FRAME)
        result = segmenter.push(frame)
        if result is not None:
            utterance = result

    assert utterance is not None
    with store.borrow(utterance.handle) as audio:
        assert isinstance(audio, bytes)
        assert audio.startswith(marker * 3), "pre-roll frames should head the utterance"


def test_short_noise_is_discarded_and_never_stored() -> None:
    """A cough is dropped without reaching the store, so there is nothing to expire."""
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=20,
        hangover_ms=40,
        min_utterance_ms=500,
        max_utterance_ms=1000,
    )
    script = [True] * 2 + [False] * 2
    segmenter, store = make_segmenter(script, config=config)

    for is_speech in script:
        assert segmenter.push(LOUD_FRAME if is_speech else SILENT_FRAME) is None

    assert len(store) == 0, "sub-minimum audio must never be stored"
    assert segmenter.buffered_frames == 0


def test_max_duration_forces_an_utterance_to_end() -> None:
    """Someone who never pauses, or a detector stuck on, must not grow a buffer forever."""
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=20,
        hangover_ms=40,
        min_utterance_ms=20,
        max_utterance_ms=100,  # 5 frames
    )
    segmenter, _ = make_segmenter([True] * 50, config=config)

    emitted = [segmenter.push(LOUD_FRAME) for _ in range(20)]
    utterances = [u for u in emitted if u is not None]

    assert utterances, "an unending speech stream must still produce utterances"
    assert all(u.ended_because is EndReason.MAX_DURATION for u in utterances)
    assert all(u.frame_count <= 5 for u in utterances)
    assert segmenter.buffered_frames < 5


def test_flush_emits_in_progress_audio_and_leaves_nothing_buffered() -> None:
    script = [False] * 3 + [True] * 3
    segmenter, store = make_segmenter(script)
    for is_speech in script:
        segmenter.push(LOUD_FRAME if is_speech else SILENT_FRAME)

    assert segmenter.in_speech
    utterance = segmenter.flush()

    assert utterance is not None
    assert utterance.ended_because is EndReason.FLUSH
    assert store.is_present(utterance.handle)
    assert segmenter.buffered_frames == 0
    assert not segmenter.in_speech


def test_flush_with_nothing_in_progress_clears_the_pre_roll() -> None:
    segmenter, store = make_segmenter([False] * 5)
    for _ in range(5):
        segmenter.push(SILENT_FRAME)

    assert segmenter.flush() is None
    assert len(store) == 0


def test_reset_drops_buffered_audio_and_detector_state() -> None:
    script = [False] * 3 + [True] * 3
    detector = ScriptedDetector(script)
    store = make_store()
    segmenter = UtteranceSegmenter(
        store=store, detector=detector, audio_format=FORMAT, config=TEST_CONFIG
    )
    for is_speech in script:
        segmenter.push(LOUD_FRAME if is_speech else SILENT_FRAME)

    assert segmenter.buffered_frames > 0
    segmenter.reset()

    assert segmenter.buffered_frames == 0
    assert not segmenter.in_speech
    assert detector.resets == 1
    assert len(store) == 0, "reset must not stash the dropped audio"


def test_segmenter_refuses_a_pre_roll_longer_than_the_retention_window() -> None:
    """The pre-roll ring is retention-by-construction only while it is the tighter bound."""
    store = make_store()
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=30_000,
        hangover_ms=40,
        min_utterance_ms=20,
        max_utterance_ms=1000,
    )
    with pytest.raises(ValueError, match="retention window"):
        UtteranceSegmenter(
            store=store, detector=ScriptedDetector([]), audio_format=FORMAT, config=config
        )


def test_segmenter_config_validation() -> None:
    with pytest.raises(ValueError, match="frame_ms"):
        SegmenterConfig(frame_ms=0)
    with pytest.raises(ValueError, match="max_utterance_ms must exceed"):
        SegmenterConfig(min_utterance_ms=500, max_utterance_ms=500)
    with pytest.raises(ValueError, match="ceiling"):
        SegmenterConfig(max_utterance_ms=120_000)
    with pytest.raises(ValueError, match="at least one frame"):
        SegmenterConfig(frame_ms=20, hangover_ms=10)


def test_utterance_metadata_carries_no_audio() -> None:
    script = [False] * 3 + [True] * 3 + [False] * 2
    segmenter, _ = make_segmenter(script)
    utterance = None
    for is_speech in script:
        result = segmenter.push(LOUD_FRAME if is_speech else SILENT_FRAME)
        if result is not None:
            utterance = result

    assert utterance is not None
    rendered = str(utterance) + repr(utterance) + str(utterance.handle)
    assert LOUD_FRAME.hex()[:32] not in rendered
    assert "captured_audio_frames" in rendered


# ======================================================================================
# Retention integration
# ======================================================================================


def test_captured_audio_expires_on_the_retention_clock() -> None:
    """The whole point: audio that reaches the store is deleted ten seconds after use."""
    clock = ManualClock()
    store = make_store(clock)
    script = [False] * 3 + [True] * 3 + [False] * 2
    segmenter, _ = make_segmenter(script, store=store)

    utterance = None
    for is_speech in script:
        result = segmenter.push(LOUD_FRAME if is_speech else SILENT_FRAME)
        if result is not None:
            utterance = result

    assert utterance is not None
    assert store.is_present(utterance.handle)

    clock.advance(9.0)
    store.reap()
    assert store.is_present(utterance.handle)

    clock.advance(1.001)
    store.reap()
    assert not store.is_present(utterance.handle), "captured audio outlived its window"


# ======================================================================================
# CaptureSession
# ======================================================================================


def build_session(
    frames: list[bytes],
    script: list[bool],
    store: EphemeralStore | None = None,
) -> tuple[CaptureSession, FakeSource, EphemeralStore]:
    target = store if store is not None else make_store()
    source = FakeSource(frames)
    session = CaptureSession(
        source=source, detector=ScriptedDetector(script), store=target, config=TEST_CONFIG
    )
    return session, source, target


def test_session_yields_utterances_and_reports_metadata_only_stats() -> None:
    script = [False] * 3 + [True] * 3 + [False] * 2
    frames = [LOUD_FRAME if s else SILENT_FRAME for s in script]
    session, source, store = build_session(frames, script)

    collected = list(session.utterances())

    assert len(collected) == 1
    assert store.is_present(collected[0].handle)
    stats = session.stats
    assert isinstance(stats, CaptureStats)
    assert stats.frames_read == len(frames)
    assert stats.utterances_emitted == 1
    assert stats.frames_invalid == 0
    assert stats.audio_seconds_seen == pytest.approx(len(frames) * FRAME_MS / 1000)
    assert source.closed == 1


def test_session_closes_the_device_and_flushes_when_the_caller_stops_early() -> None:
    """Leaving a microphone open after a session is a privacy problem, not just a leak."""
    script = [True] * 40
    frames = [LOUD_FRAME] * 40
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=20,
        hangover_ms=40,
        min_utterance_ms=20,
        max_utterance_ms=100,
    )
    source = FakeSource(frames)
    store = make_store()
    session = CaptureSession(
        source=source, detector=ScriptedDetector(script), store=store, config=config
    )

    with session:
        for _ in session.utterances():
            break  # caller loses interest halfway through

    assert source.closed >= 1, "the device must be released when the session ends"


def test_session_emits_the_tail_when_the_source_ends_mid_utterance() -> None:
    """Someone stopping mid-sentence should still get that sentence.

    The source runs out while speech is in progress, so no hangover silence ever arrives.
    An earlier version stored this audio without yielding it, which retained content for
    ten seconds to no purpose. Added after mutation testing showed nothing caught it.
    """
    script = [False, True, True, True]
    frames = [SILENT_FRAME, LOUD_FRAME, LOUD_FRAME, LOUD_FRAME]
    session, source, store = build_session(frames, script)

    collected = list(session.utterances())

    assert len(collected) == 1, "the tail utterance must be delivered, not just stored"
    assert collected[0].ended_because is EndReason.FLUSH
    assert store.is_present(collected[0].handle)
    assert session.stats.utterances_emitted == 1
    assert source.closed == 1


def test_session_discards_buffered_audio_when_the_caller_stops_early() -> None:
    """An abandoned session stores nothing extra; nobody is waiting for that audio."""
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=20,
        hangover_ms=40,
        min_utterance_ms=20,
        max_utterance_ms=100,  # 5 frames, so utterances complete quickly
    )
    frames = [LOUD_FRAME] * 40
    source = FakeSource(frames)
    store = make_store()
    session = CaptureSession(
        source=source, detector=ScriptedDetector([True] * 40), store=store, config=config
    )

    delivered = 0
    with session:
        for _ in session.utterances():
            delivered += 1
            break  # caller loses interest with speech still buffered

    assert delivered == 1
    assert source.closed >= 1, "the device must be released when the session ends"
    assert session.buffered_frames == 0, "a finished session must hold no captured audio"
    assert len(store) == delivered, (
        "an abandoned session must not store the audio it was still buffering"
    )


def test_a_device_failing_mid_utterance_discards_the_buffered_audio() -> None:
    """The case where discard-versus-store actually differs.

    At every yield point the segmenter has just emitted, so its buffer is empty and the
    choice is invisible. It becomes visible when the source fails mid-speech: there is
    real audio buffered, nobody is waiting for it, and storing it would retain content for
    ten seconds that no one will ever read.
    """
    frames = [SILENT_FRAME, LOUD_FRAME, LOUD_FRAME, LOUD_FRAME, LOUD_FRAME]
    source = FakeSource(frames, fail_after=4)
    store = make_store()
    session = CaptureSession(
        source=source,
        detector=ScriptedDetector([False, True, True, True, True]),
        store=store,
        config=TEST_CONFIG,
    )

    with pytest.raises(OSError, match="disconnected"):
        list(session.utterances())

    assert session.buffered_frames == 0, "audio was left buffered after a failed session"
    assert len(store) == 0, "audio nobody is waiting for must be discarded, not stored"
    assert source.closed == 1, "the device is released even on the failure path"


def test_session_close_is_idempotent() -> None:
    session, source, _ = build_session([SILENT_FRAME], [False])
    session.close()
    session.close()
    assert source.closed == 1


def test_session_drops_malformed_frames_and_keeps_going() -> None:
    """Real hardware produces a truncated buffer occasionally; that is not a crash.

    Note the script has one fewer entry than there are frames: a malformed frame is
    rejected by format validation before the detector is consulted, so it never consumes
    detector state. That is the intended behaviour — a corrupt buffer should not be fed to
    a detector that would have to guess what it contained.
    """
    frames = [SILENT_FRAME, b"\x01", LOUD_FRAME, LOUD_FRAME, LOUD_FRAME, SILENT_FRAME, SILENT_FRAME]
    script = [False, True, True, True, False, False]  # the six frames that reach it
    session, _, _ = build_session(frames, script)

    collected = list(session.utterances())

    assert session.stats.frames_read == 7
    assert session.stats.frames_invalid == 1
    assert len(collected) == 1, "one bad buffer must not lose the utterance around it"


def test_session_gives_up_on_a_device_that_only_produces_garbage() -> None:
    """Continuing to read from a broken device is not resilience."""
    frames = [b"\x01"] * 20
    session, source, _ = build_session(frames, [False] * 20)

    with pytest.raises(CaptureError, match="malformed"):
        list(session.utterances())

    assert source.closed == 1, "the device is still released on the failure path"
