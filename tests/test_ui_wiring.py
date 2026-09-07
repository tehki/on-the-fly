"""Tests for how the desktop application wires its capture path.

No Qt and no display. `ui/app.py` is the composition root for the window in the same sense
`app/cli.py` is one for the command line, and the order it wraps a capture source in is a
decision with a user-visible consequence — not an implementation detail.

It was a comment until 2026-09-07. The two defects found in this project's capture path that
week were both places where a property everyone believed was guaranteed by prose.
"""

from __future__ import annotations

from collections.abc import Iterator

from on_the_fly.domain.audio import AudioFormat
from on_the_fly.domain.audio.levels import InputQuality, LevelWatchingSource
from on_the_fly.domain.audio.settling import SettlingSource
from on_the_fly.ui.app import build_capture_stack

FORMAT = AudioFormat()
FRAME_BYTES = FORMAT.frame_bytes(20)
SAMPLES_PER_FRAME = FRAME_BYTES // 2


class FakeSource:
    """Replays scripted frames, as a capture device would."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.closed = 0

    @property
    def audio_format(self) -> AudioFormat:
        return FORMAT

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def close(self) -> None:
        self.closed += 1


def rail_pinned(count: int) -> list[bytes]:
    """A cold capture: every sample at the negative rail, which reads as clipping.

    ADR 0020 measured this on the reference machine — DC -1.0, 100% of samples clipped, no
    signal at all, for about 1.8 seconds before the analog path centres.
    """
    return [(-32768).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME] * count


def speech(count: int) -> list[bytes]:
    """Ordinary centred audio, alternating either side of zero so it is not silence."""
    sample = (6000).to_bytes(2, "little", signed=True) + (-6000).to_bytes(2, "little", signed=True)
    return [sample * (SAMPLES_PER_FRAME // 2)] * count


def test_the_stack_is_the_level_monitor_over_settling_over_the_device() -> None:
    source = FakeSource([])

    stack = build_capture_stack(source)

    assert isinstance(stack, LevelWatchingSource)
    assert isinstance(stack.source, SettlingSource)
    assert stack.source.source is source


def test_a_cold_start_does_not_report_the_microphone_as_broken() -> None:
    """The consequence of the order, not just its shape.

    A cold capture opens pinned at the rail. Settling discards that before the level monitor
    ever sees it, so the verdict describes the microphone rather than the analog path
    powering up.
    """
    source = FakeSource(rail_pinned(90) + speech(200))

    stack = build_capture_stack(source)
    for _ in stack.frames():
        pass

    assert stack.overall_level.quality is InputQuality.OK


def test_the_reverse_order_would_condemn_the_microphone() -> None:
    """Why the order is worth a test rather than a comment.

    With the monitor underneath, the rail-pinned transient is measured before it is
    discarded — and `overall` judges a recording on the worst window it contains, so that
    verdict stands for the whole session rather than the first two seconds.
    """
    source = FakeSource(rail_pinned(90) + speech(200))

    wrong_way_round = SettlingSource(LevelWatchingSource(source))
    for _ in wrong_way_round.frames():
        pass

    monitor = wrong_way_round.source
    assert isinstance(monitor, LevelWatchingSource)
    assert monitor.overall_level.quality is InputQuality.CLIPPING


def test_closing_the_stack_releases_the_device() -> None:
    """Through both wrappers: neither may swallow the close."""
    source = FakeSource([])

    build_capture_stack(source).close()

    assert source.closed == 1
