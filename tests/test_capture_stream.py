"""Tests for the callback-driven capture stream (ADR 0015).

`_SoundDeviceStream` takes its PortAudio stream and its queue as arguments, so the whole
thing is testable without a sound card. What is worth pinning is not that bytes move — it is
the behaviour that only shows up when something goes wrong: a consumer falling behind, a
device disappearing mid-sentence, audio left in the queue at close.
"""

from __future__ import annotations

import queue

import pytest

from on_the_fly.infrastructure.audio.backend import AudioDeviceError, _SoundDeviceStream


class FakePortAudioStream:
    """Records lifecycle calls. Raises on demand, the way a real device does."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.started = False
        self.stopped = False
        self.closed = False
        self.fail_start = fail_start

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("device busy")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def build(
    maxsize: int = 4,
) -> tuple[_SoundDeviceStream, FakePortAudioStream, queue.Queue[bytes]]:
    blocks: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)
    raw = FakePortAudioStream()
    return _SoundDeviceStream(raw, blocks), raw, blocks


# --------------------------------------------------------------------------------------
# Ordinary flow
# --------------------------------------------------------------------------------------


def test_a_delivered_block_is_returned() -> None:
    stream, _, blocks = build()
    blocks.put(b"\x01\x02" * 320)

    data, overflowed = stream.read(320)

    assert data == b"\x01\x02" * 320
    assert overflowed is False


def test_blocks_come_out_in_the_order_they_arrived() -> None:
    """Speech is sequential. Reordering it would be worse than losing it."""
    stream, _, blocks = build()
    for i in (1, 2, 3):
        blocks.put(bytes([i]) * 640)

    assert [stream.read(320)[0][0] for _ in range(3)] == [1, 2, 3]


def test_starting_twice_is_harmless() -> None:
    stream, raw, _ = build()

    stream.start()
    stream.start()

    assert raw.started is True


def test_a_device_that_will_not_start_raises_the_project_error_type() -> None:
    blocks: queue.Queue[bytes] = queue.Queue(maxsize=4)
    stream = _SoundDeviceStream(FakePortAudioStream(fail_start=True), blocks)

    with pytest.raises(AudioDeviceError, match="could not start"):
        stream.start()


# --------------------------------------------------------------------------------------
# Falling behind
# --------------------------------------------------------------------------------------


def test_a_dropped_block_is_reported_as_an_overflow() -> None:
    """Lost audio is a dropped word. It is counted, never silently absorbed."""
    stream, _, blocks = build()
    blocks.put(b"\x00" * 640)
    stream.note_drop()

    _, overflowed = stream.read(320)

    assert overflowed is True


def test_portaudio_reporting_its_own_overflow_is_surfaced() -> None:
    """The driver discarding input counts too, not only blocks this project dropped."""
    stream, _, blocks = build()
    blocks.put(b"\x00" * 640)
    stream.note_status_overflow()

    assert stream.read(320)[1] is True


def test_the_overflow_flag_clears_after_being_reported() -> None:
    """Otherwise one drop would mark every later frame and the count would be useless."""
    stream, _, blocks = build()
    blocks.put(b"\x00" * 640)
    blocks.put(b"\x00" * 640)
    stream.note_drop()

    first = stream.read(320)[1]
    second = stream.read(320)[1]

    assert first is True
    assert second is False


def test_the_queue_bound_is_two_seconds_of_audio() -> None:
    """Bounded on purpose: unbounded buffering trades a drop-out for stale audio and a leak."""
    assert _SoundDeviceStream.MAX_BLOCKS == 100


# --------------------------------------------------------------------------------------
# Things going wrong
# --------------------------------------------------------------------------------------


def test_a_device_that_stops_delivering_becomes_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running stream delivers blocks whether or not anyone speaks, so silence is a fault.

    Unplugging a microphone mid-sentence lands here. Under blocking reads there was no way
    to tell a disconnected device from a patient one.
    """
    stream, _, blocks = build()

    def instant_timeout(*args: object, **kwargs: object) -> bytes:
        raise queue.Empty

    monkeypatch.setattr(blocks, "get", instant_timeout)

    with pytest.raises(AudioDeviceError, match="stopped delivering audio"):
        stream.read(320)


def test_reading_after_close_is_refused() -> None:
    stream, _, _ = build()
    stream.close()

    with pytest.raises(AudioDeviceError, match="closed"):
        stream.read(320)


def test_starting_after_close_is_refused() -> None:
    stream, _, _ = build()
    stream.close()

    with pytest.raises(AudioDeviceError, match="closed"):
        stream.start()


# --------------------------------------------------------------------------------------
# Closing
# --------------------------------------------------------------------------------------


def test_close_stops_and_closes_the_device() -> None:
    stream, raw, _ = build()
    stream.start()

    stream.close()

    assert raw.stopped is True
    assert raw.closed is True


def test_close_discards_queued_audio() -> None:
    """Captured audio is EPHEMERAL and has no reason to outlive its device."""
    stream, _, blocks = build()
    for _ in range(3):
        blocks.put(b"\x00" * 640)

    stream.close()

    assert blocks.empty()


def test_close_is_idempotent() -> None:
    stream, raw, _ = build()

    stream.close()
    stream.close()

    assert raw.closed is True


def test_close_survives_a_device_that_fails_to_stop() -> None:
    """A failure here would mask whatever error caused the shutdown."""
    blocks: queue.Queue[bytes] = queue.Queue(maxsize=4)
    raw = FakePortAudioStream()

    def explode() -> None:
        raise RuntimeError("device already gone")

    raw.stop = explode  # type: ignore[method-assign]
    stream = _SoundDeviceStream(raw, blocks)

    stream.close()  # must not raise

    assert raw.closed is True
