"""Tests for audio capture, voice activity detection and utterance segmentation.

Segmentation is tested against a scripted detector rather than the energy one. The two
concerns are separate: whether a frame contains speech, and how frames are cut into
utterances. Testing them together would mean a change to the detector's sensitivity broke
segmentation tests, which is exactly the coupling `ports.py` exists to avoid.

The energy detector is tested on its own, against synthesised audio.
"""

from __future__ import annotations

import dataclasses
from array import array
from collections.abc import Iterator

import pytest

from on_the_fly.domain.audio import (
    ABSOLUTE_MAX_UTTERANCE_MS,
    DEFAULT_MAX_UTTERANCE_MS,
    INT16_NORMALISATION_SCALE,
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
from on_the_fly.domain.audio.levels import FULL_SCALE
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


def test_half_a_sample_is_not_a_whole_number_of_samples() -> None:
    """The bug this check had until 2026-09-10: it counted bytes.

    At 11.025 kHz a 20 ms frame is 220.5 samples — 441 bytes, which is a whole number of
    bytes and half a sample. The check passed, `WavFileSource` asked for 220 samples, found
    440 bytes where it expected 441, and stopped. A file full of speech produced no audio at
    all and no error.
    """
    voice_memo = AudioFormat(sample_rate_hz=11_025)

    with pytest.raises(ValueError, match="whole number of samples"):
        voice_memo.frame_bytes(20)


def test_the_refusal_names_a_duration_that_works() -> None:
    """A rule the reader has to solve is worse than an answer, and every rate consumer
    hardware produces has one."""
    voice_memo = AudioFormat(sample_rate_hz=11_025)

    with pytest.raises(ValueError, match=r"try --frame-ms (\d+)") as raised:
        voice_memo.frame_bytes(20)

    suggested = int(str(raised.value).split("--frame-ms ")[1])
    assert voice_memo.frame_bytes(suggested) == 11_025 * suggested // 1000 * 2


@pytest.mark.parametrize("rate", [8_000, 16_000, 22_050, 32_000, 44_100, 48_000])
def test_the_rates_that_already_worked_are_unchanged(rate: int) -> None:
    """The fix must not start refusing what it accepted: every rate consumer hardware and
    every published test set uses lands on a whole sample at 20 ms."""
    assert AudioFormat(sample_rate_hz=rate).frame_bytes(20) == rate * 20 // 1000 * 2


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


def test_a_detector_started_mid_speech_recovers() -> None:
    """The defect ADR 0025 fixes: seeded on speech, the detector was born deaf.

    Someone who starts the application already talking seeds the floor three times too high,
    so nothing they say clears the threshold. Before the recovery rate it stayed that way.
    """
    detector = EnergyVoiceActivityDetector()
    speech = frame_of(2400)
    quiet = frame_of(200)

    # Capture begins in the middle of a sentence.
    assert detector.is_speech(speech) is False, "seeded on this frame, so it cannot be speech"
    for _ in range(20):
        detector.is_speech(speech)

    # One pause between words is enough to learn what the room actually sounds like.
    for _ in range(10):
        detector.is_speech(quiet)

    assert detector.is_speech(speech) is True, "still deaf after a pause"


def test_the_floor_falls_faster_than_it_rises() -> None:
    """The asymmetry is the fix, so it is asserted rather than assumed."""
    detector = EnergyVoiceActivityDetector()
    detector.is_speech(frame_of(1000))
    started = detector.noise_floor

    detector.is_speech(frame_of(0))
    fell = started - detector.noise_floor

    riser = EnergyVoiceActivityDetector()
    riser.is_speech(frame_of(0))
    before = riser.noise_floor
    riser.is_speech(frame_of(100))
    rose = riser.noise_floor - before

    assert fell > rose * 5


def test_continuous_speech_does_not_deafen_the_detector() -> None:
    """A missed frame counts as silence, which lifts the floor, which misses the next one.

    Measured on sixteen seconds of continuous recorded speech, the symmetric version scored
    an F1 of 31.8% against a reference labelling even from a clean start (ADR 0025).
    """
    detector = EnergyVoiceActivityDetector()
    loud = frame_of(2400)
    # Speech is not a constant: real syllables dip between them, and those dips are what a
    # falling floor uses to stay honest.
    heard = 0
    for index in range(400):
        frame = loud if index % 5 else frame_of(600)
        if detector.is_speech(frame):
            heard += 1

    assert heard > 250, f"the detector went deaf during continuous speech ({heard}/400)"


def test_room_tone_is_still_not_speech() -> None:
    """The falling floor must not make an idle microphone audible."""
    detector = EnergyVoiceActivityDetector()
    for _ in range(200):
        detector.is_speech(frame_of(200))

    assert detector.is_speech(frame_of(200)) is False
    # And digital near-silence stays below the absolute floor however low the floor goes.
    for _ in range(200):
        detector.is_speech(frame_of(1))
    assert detector.is_speech(frame_of(100)) is False


def test_a_recovery_rate_below_the_adaptation_rate_is_refused() -> None:
    """Reversed, the detector deafens itself faster than it recovers."""
    with pytest.raises(ValueError, match="recovery_rate"):
        EnergyVoiceActivityDetector(adaptation_rate=0.5, recovery_rate=0.05)
    with pytest.raises(ValueError, match="recovery_rate"):
        EnergyVoiceActivityDetector(recovery_rate=0.0)
    with pytest.raises(ValueError, match="recovery_rate"):
        EnergyVoiceActivityDetector(recovery_rate=1.5)


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


# ======================================================================================
# The boundaries of the endpointing decisions
#
# What counts as speech, and where an utterance ends, are decided by a handful of
# comparisons. Mutation testing over `domain/audio/` found that most of them could be
# flipped without a test noticing: 13 of 28 sites in vad.py and 22 of 33 in segmenter.py
# survived. The guards were tested; the exact points they turn on were not.
#
# Each is an off-by-one in a decision a listener hears — a syllable clipped, a pause read as
# the end of a turn — and one of them is a retention guard.
# ======================================================================================


# --- what counts as speech --------------------------------------------------------------


def test_a_frame_exactly_at_the_threshold_is_not_speech() -> None:
    """`rms > threshold`, not `>=`. A frame no louder than the threshold having been
    declared speech is how a detector reports a whole quiet meeting as talking.

    Seeded with silence first, because the very first frame defines the starting floor —
    handing it the frame under test would make that frame its own threshold.
    """
    detector = EnergyVoiceActivityDetector(absolute_silence_rms=1000.0, speech_factor=3.0)
    assert not detector.is_speech(SILENT_FRAME), "seeds the floor at zero"

    assert not detector.is_speech(frame_of(1000)), "exactly at the absolute floor"
    assert detector.is_speech(frame_of(1001)), "one step above it"


def test_an_absolute_silence_floor_of_zero_is_allowed() -> None:
    """Zero is the boundary of the guard, and a legitimate setting: it means "trust the
    adaptive floor alone", which is what a test rig with no room tone wants."""
    assert EnergyVoiceActivityDetector(absolute_silence_rms=0.0).noise_floor == 0.0


def test_a_negative_absolute_silence_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute_silence_rms"):
        EnergyVoiceActivityDetector(absolute_silence_rms=-1.0)


@pytest.mark.parametrize("factor", [1.0, 0.5])
def test_a_speech_factor_that_does_not_exceed_one_is_refused(factor: float) -> None:
    """At exactly 1.0 the threshold is the noise floor itself, so room tone is speech."""
    with pytest.raises(ValueError, match="speech_factor"):
        EnergyVoiceActivityDetector(speech_factor=factor)


def test_a_speech_factor_just_above_one_is_allowed() -> None:
    """The guard is exclusive, and the first value above it has to work."""
    assert EnergyVoiceActivityDetector(speech_factor=1.001) is not None


@pytest.mark.parametrize("rate", [0.0, 1.0, -0.1, 1.1])
def test_an_adaptation_rate_outside_the_open_unit_interval_is_refused(rate: float) -> None:
    """Zero never adapts; one adopts every frame as the floor, including a shout."""
    with pytest.raises(ValueError, match="adaptation_rate"):
        EnergyVoiceActivityDetector(adaptation_rate=rate, recovery_rate=1.0)


def test_a_recovery_rate_of_exactly_one_is_allowed() -> None:
    """Unlike adaptation, recovery may take a frame whole: a frame quieter than the floor
    is evidence the floor is wrong, and acting on it immediately is the safe direction."""
    assert EnergyVoiceActivityDetector(recovery_rate=1.0) is not None


@pytest.mark.parametrize("rate", [0.0, 1.1])
def test_a_recovery_rate_outside_its_range_is_refused(rate: float) -> None:
    """Matched on this guard's own words. "recovery_rate" alone would also match the
    asymmetry rule below it, so a zero rate would look refused for the wrong reason.
    """
    with pytest.raises(ValueError, match="above 0 and at most 1"):
        EnergyVoiceActivityDetector(recovery_rate=rate, adaptation_rate=0.0001)


def test_recovery_may_equal_adaptation_but_not_fall_below_it() -> None:
    """ADR 0025's asymmetry, at the point it turns on.

    Reversed, the detector deafens itself faster than it recovers — the defect that ADR
    exists to fix. Equal is the boundary and is permitted: symmetric is not backwards.
    """
    assert EnergyVoiceActivityDetector(adaptation_rate=0.05, recovery_rate=0.05) is not None

    with pytest.raises(ValueError, match="recovery_rate"):
        EnergyVoiceActivityDetector(adaptation_rate=0.05, recovery_rate=0.049)


# --- where an utterance ends --------------------------------------------------------------


@pytest.mark.parametrize("field", ["frame_ms", "pre_roll_ms", "hangover_ms", "min_utterance_ms"])
def test_a_zero_timing_is_refused(field: str) -> None:
    """Zero is the boundary of "must be positive", and every one of these divides or counts
    frames, so zero is not a small value but a broken one."""
    with pytest.raises(ValueError, match=field):
        SegmenterConfig(**{field: 0})


def test_a_maximum_equal_to_the_minimum_is_refused() -> None:
    """Equal leaves no window at all: every utterance would be both too short and too long."""
    with pytest.raises(ValueError, match="max_utterance_ms"):
        SegmenterConfig(min_utterance_ms=500, max_utterance_ms=500)


def test_a_maximum_exactly_at_the_ceiling_is_allowed() -> None:
    """The ceiling is inclusive. Refusing here would make the documented limit unreachable."""
    assert SegmenterConfig(max_utterance_ms=ABSOLUTE_MAX_UTTERANCE_MS).max_utterance_ms == (
        ABSOLUTE_MAX_UTTERANCE_MS
    )


def test_a_maximum_one_millisecond_over_the_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="ceiling"):
        SegmenterConfig(max_utterance_ms=ABSOLUTE_MAX_UTTERANCE_MS + 1)


def test_a_hangover_of_exactly_one_frame_is_allowed() -> None:
    """One frame is the smallest hangover that can exist; shorter is not a fast endpoint,
    it is a hangover that never fires."""
    assert SegmenterConfig(frame_ms=20, hangover_ms=20).hangover_frames == 1

    with pytest.raises(ValueError, match="hangover_ms"):
        SegmenterConfig(frame_ms=20, hangover_ms=19)


@pytest.mark.parametrize(
    "attribute", ["pre_roll_frames", "hangover_frames", "max_utterance_frames"]
)
def test_a_frame_count_never_rounds_down_to_nothing(attribute: str) -> None:
    """Integer division would give zero for any duration under one frame. A pre-roll ring of
    length zero keeps no audio, and a maximum of zero frames ends every utterance instantly.
    """
    config = SegmenterConfig(
        frame_ms=100, pre_roll_ms=50, hangover_ms=100, min_utterance_ms=1, max_utterance_ms=50
    )
    assert config.pre_roll_ms < config.frame_ms, "the case the floor exists for"

    assert getattr(config, attribute) == 1, "the floor is one frame, not more and not none"


def test_the_utterance_ends_on_the_hangover_frame_and_not_before() -> None:
    """The endpoint decision itself. One frame early clips the speaker mid-pause; one frame
    late is a turn that hangs."""
    hangover_frames = TEST_CONFIG.hangover_frames
    segmenter, _ = make_segmenter([True] + [False] * hangover_frames)

    assert segmenter.push(frame_of(9000)) is None
    for index in range(hangover_frames - 1):
        assert segmenter.push(SILENT_FRAME) is None, f"ended {hangover_frames - index} early"

    assert segmenter.push(SILENT_FRAME) is not None, "did not end on the hangover frame"


def emit_a_two_frame_utterance(min_utterance_ms: int) -> object | None:
    """One speech frame plus the hangover frame that ends it: 40ms of audio."""
    config = SegmenterConfig(
        frame_ms=20,
        pre_roll_ms=20,
        hangover_ms=20,
        min_utterance_ms=min_utterance_ms,
        max_utterance_ms=200,
    )
    segmenter, _ = make_segmenter([True, False], config=config)
    segmenter.push(frame_of(9000))
    return segmenter.push(SILENT_FRAME)


def test_an_utterance_exactly_at_the_minimum_length_is_kept() -> None:
    """The minimum is inclusive. Exclusive, the shortest configurable utterance would be
    one that can never be produced."""
    assert emit_a_two_frame_utterance(min_utterance_ms=40) is not None


def test_an_utterance_one_millisecond_under_the_minimum_is_dropped() -> None:
    """Dropped without ever being stored, so there is nothing to expire."""
    assert emit_a_two_frame_utterance(min_utterance_ms=41) is None


def test_the_default_maximum_sits_under_the_absolute_ceiling() -> None:
    """The ceiling is what a configuration may not exceed; the default is what it gets
    without asking. A default above the ceiling would make the out-of-the-box segmenter
    refuse to construct."""
    assert DEFAULT_MAX_UTTERANCE_MS <= ABSOLUTE_MAX_UTTERANCE_MS
    assert SegmenterConfig().max_utterance_ms == DEFAULT_MAX_UTTERANCE_MS


def test_the_utterance_is_cut_on_the_frame_that_reaches_the_maximum() -> None:
    """The other way an utterance ends. One frame late and the ceiling is not the ceiling.

    Speech that never stops, so only the maximum can end it.
    """
    config = SegmenterConfig(
        frame_ms=20, pre_roll_ms=20, hangover_ms=20, min_utterance_ms=20, max_utterance_ms=100
    )
    assert config.max_utterance_frames == 5
    segmenter, _ = make_segmenter([True] * 5, config=config)

    for index in range(4):
        assert segmenter.push(frame_of(9000)) is None, f"cut at frame {index + 1} of 5"

    utterance = segmenter.push(frame_of(9000))
    assert utterance is not None, "did not cut on the frame that reached the maximum"
    assert utterance.ended_because is EndReason.MAX_DURATION


def test_the_maximum_also_cuts_an_utterance_during_its_trailing_silence() -> None:
    """The same ceiling is checked on both paths, and they are two comparisons.

    Trailing silence is kept as part of the utterance until the speaker is known to have
    stopped, so it counts toward the maximum — and an utterance can reach the ceiling while
    still inside its hangover. Ending it as SILENCE there would report the speaker as having
    finished when what actually happened is that the buffer filled.
    """
    config = SegmenterConfig(
        frame_ms=20, pre_roll_ms=20, hangover_ms=60, min_utterance_ms=20, max_utterance_ms=60
    )
    assert config.hangover_frames == 3 and config.max_utterance_frames == 3
    segmenter, _ = make_segmenter([True, False, False], config=config)

    assert segmenter.push(frame_of(9000)) is None
    assert segmenter.push(SILENT_FRAME) is None, "cut before the maximum"

    utterance = segmenter.push(SILENT_FRAME)
    assert utterance is not None, "did not cut on the frame that reached the maximum"
    assert utterance.ended_because is EndReason.MAX_DURATION, (
        "the buffer filling is not the speaker stopping"
    )


def test_resetting_makes_the_next_frame_seed_the_floor_again() -> None:
    """`reset()` is for a new capture session. A detector that kept the old room's floor
    would carry one room's noise into another's."""
    detector = EnergyVoiceActivityDetector(absolute_silence_rms=1.0)
    detector.is_speech(frame_of(9000))
    assert detector.noise_floor == pytest.approx(9000.0)

    detector.reset()
    assert detector.noise_floor == 0.0
    detector.is_speech(frame_of(500))

    assert detector.noise_floor == pytest.approx(500.0), "the next frame did not reseed"


def test_a_pre_roll_as_long_as_the_retention_window_is_allowed() -> None:
    """The ring is retention-by-construction only while it is the tighter bound, and equal
    is still bounded. Refusing here would forbid the longest legitimate pre-roll."""
    store = EphemeralStore("on-the-fly", clock=ManualClock())
    config = SegmenterConfig(pre_roll_ms=int(store.retention_seconds * 1000))

    assert (
        UtteranceSegmenter(
            store=store, detector=ScriptedDetector([]), audio_format=FORMAT, config=config
        )
        is not None
    )


def test_a_pre_roll_longer_than_the_retention_window_is_refused() -> None:
    """One millisecond over and the ring becomes the longer-lived copy of captured audio,
    which is content escaping the retention rule rather than a tuning mistake."""
    store = EphemeralStore("on-the-fly", clock=ManualClock())
    config = SegmenterConfig(pre_roll_ms=int(store.retention_seconds * 1000) + 20)

    with pytest.raises(ValueError, match="retention window"):
        UtteranceSegmenter(
            store=store, detector=ScriptedDetector([]), audio_format=FORMAT, config=config
        )


# ======================================================================================
# The arithmetic everything else is measured in
#
# `AudioFormat` turns durations into byte counts and back. Every frame size, every reported
# duration and every real-time factor in this project is derived from it, so a guard that
# is off by one here is off by one everywhere. Mutation testing found each of its
# boundaries unpinned.
# ======================================================================================


def test_the_smallest_frame_duration_is_one_millisecond() -> None:
    """The guard is on zero and below. One millisecond is a legal, if unusual, frame, and
    the refusal must not creep up into durations somebody chose."""
    assert AudioFormat().frame_bytes(1) == 32

    with pytest.raises(ValueError, match="must be positive"):
        AudioFormat().frame_bytes(0)


def test_a_negative_frame_duration_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        AudioFormat().frame_bytes(-20)


def test_a_sample_rate_of_one_hertz_is_allowed() -> None:
    """Absurd and legal. The guard is against zero and negative rates, which cannot
    describe audio at all; one hertz merely describes it badly."""
    assert AudioFormat(sample_rate_hz=1).bytes_per_second == 2


def test_a_sample_rate_of_zero_is_refused() -> None:
    """Zero would make every frame zero bytes long, and a reader looping on whole frames
    would never advance."""
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        AudioFormat(sample_rate_hz=0)


def test_no_audio_at_all_lasts_no_time() -> None:
    """Zero is the boundary of the guard and a real answer: an empty buffer is zero
    seconds, not an error. A run that read nothing reports that duration."""
    assert AudioFormat().duration_seconds(0) == 0.0


def test_a_negative_byte_count_is_refused() -> None:
    """There is no such buffer, and returning a negative duration would put a negative
    real-time factor in front of a reader."""
    with pytest.raises(ValueError, match="cannot be negative"):
        AudioFormat().duration_seconds(-2)


def test_a_format_is_frozen_so_a_frame_size_cannot_change_under_a_reader() -> None:
    """Frame sizes are computed from it once and used for the length of a run."""
    fmt = AudioFormat()

    with pytest.raises(dataclasses.FrozenInstanceError):
        fmt.sample_rate_hz = 8000  # type: ignore[misc]


# ======================================================================================
# When a capture device is declared failed
#
# "A device that produces malformed buffers occasionally is a nuisance; one that does it
# continuously is broken." Both halves of that are decided by one counter and one
# comparison, and mutation testing found neither pinned — including the reset that is the
# entire difference between "occasionally" and "continuously".
# ======================================================================================


BAD_FRAME = b"\x01"  # an odd byte count: not a whole int16 sample


def session_over(frames: list[bytes], *, limit: int) -> CaptureSession:
    return CaptureSession(
        source=FakeSource(frames),
        detector=ScriptedDetector([False] * len(frames)),
        store=make_store(),
        config=TEST_CONFIG,
        max_consecutive_invalid_frames=limit,
    )


def test_the_device_is_failed_on_the_frame_that_reaches_the_limit() -> None:
    """Not one after it. The limit is the number of malformed buffers in a row that is
    treated as a broken device, and a run one short of it is still a nuisance."""
    session = session_over([BAD_FRAME] * 3, limit=3)

    with pytest.raises(CaptureError, match="3 malformed"):
        list(session.utterances())


def test_a_run_one_short_of_the_limit_is_tolerated() -> None:
    session = session_over([BAD_FRAME] * 2 + [SILENT_FRAME], limit=3)

    assert list(session.utterances()) == []
    assert session.stats.frames_invalid == 2


def test_a_good_frame_resets_the_run_of_bad_ones() -> None:
    """The whole of "consecutive". Without the reset the tenth malformed buffer of a long
    session would fail a device that had been working between them — which is exactly the
    occasional nuisance the limit is written to tolerate.
    """
    frames = [BAD_FRAME] * 2 + [SILENT_FRAME] + [BAD_FRAME] * 2 + [SILENT_FRAME]
    session = session_over(frames, limit=3)

    assert list(session.utterances()) == []
    assert session.stats.frames_invalid == 4, "four bad buffers, never three in a row"


def test_a_limit_of_one_fails_on_the_first_malformed_buffer() -> None:
    """One is the smallest limit that can exist, and means no tolerance at all."""
    with pytest.raises(CaptureError, match="1 malformed"):
        list(session_over([BAD_FRAME, SILENT_FRAME], limit=1).utterances())


def test_a_limit_of_zero_is_refused() -> None:
    """A device would be failed before it had produced anything."""
    with pytest.raises(ValueError, match="max_consecutive_invalid_frames"):
        session_over([SILENT_FRAME], limit=0)


def test_a_session_that_read_nothing_reports_zeros() -> None:
    """The counters start where a reader would assume, and are metadata only."""
    session = session_over([], limit=3)
    list(session.utterances())

    assert session.stats == CaptureStats()
    assert (session.stats.frames_read, session.stats.frames_invalid) == (0, 0)
    assert (session.stats.utterances_emitted, session.stats.audio_seconds_seen) == (0, 0.0)


def test_capture_stats_are_frozen_so_a_reported_count_cannot_be_edited() -> None:
    """They are handed to callers and printed; a mutable count is a count that can drift
    from what was actually seen."""
    stats = CaptureStats(frames_read=3)

    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.frames_read = 99  # type: ignore[misc]


# ======================================================================================
# Two "full scale" numbers, one count apart, both correct
#
# `INT16_NORMALISATION_SCALE` is 32768.0 and `levels.FULL_SCALE` is 32767.0. A number that
# differs by one from another with a similar name looks like a typo, and tidying either
# into the other would be a quiet correctness change in whichever it touched. The comments
# say why; these say the same thing in a form that fails.
# ======================================================================================


def test_normalising_maps_the_whole_int16_range_into_minus_one_to_one() -> None:
    """What every model in this project takes. The most negative sample is exactly -1.0 and
    the most positive lands just short of 1.0, which is what [-1, 1) means."""
    assert -32768 / INT16_NORMALISATION_SCALE == -1.0
    assert 32767 / INT16_NORMALISATION_SCALE < 1.0
    assert 32767 / INT16_NORMALISATION_SCALE == pytest.approx(1.0, abs=1e-4)


def test_metering_calls_the_loudest_representable_sample_full() -> None:
    """The meter's question is different: how close is this to the loudest thing that can
    be represented. For a maximum positive sample the answer is 1.0, not 0.99997."""
    assert 32767 / FULL_SCALE == 1.0


def test_the_two_scales_differ_by_exactly_one_count() -> None:
    """Asserted so the relationship is a decision rather than a coincidence two files apart.

    If someone unifies them, this fails and sends them to the comments explaining why the
    two questions have different answers.
    """
    assert INT16_NORMALISATION_SCALE - FULL_SCALE == 1.0


def test_normalising_never_reaches_one_for_any_representable_sample() -> None:
    """The property the half-open interval rests on, over the whole type rather than its
    endpoints — a model handed 1.0 exactly is a model handed something out of range."""
    for sample in (-32768, -1, 0, 1, 32766, 32767):
        assert -1.0 <= sample / INT16_NORMALISATION_SCALE < 1.0
