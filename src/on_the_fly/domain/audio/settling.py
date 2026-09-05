"""Discarding the capture path's power-up transient (ADR 0020).

On the reference machine the first one and a half seconds of a *cold* capture session are
not audio. The analog input powers up when the stream opens, and the converter sits pinned
at the negative rail while its DC-blocking servo charges:

```text
   ms      dc   ac_rms   clip%
    0  -0.4212   0.5242  25.69%
  100  -1.0000   0.0000 100.00%     <- the rail, exactly, for half a second
  300  -0.9993   0.0097  97.81%
  700  -0.7175   0.3113  16.88%
 1300  -0.1370   0.3977   0.12%
 1800  -0.0194   0.3948   0.06%     <- settled
```

`arecord` shows the same shape, so this is the hardware and its driver rather than anything
this project does. It is *cold-start only*: a second session opened moments later starts
clean, because the codec is still powered.

Two things went wrong because nothing accounted for it. The recogniser was handed that
audio and had to make words out of a square wave; and it is the measurement ADR 0019 read
as evidence about the microphone's gain, which sent that decision to a conclusion its own
numbers do not support (see ADR 0020).

**Settling is adaptive rather than a fixed delay**, because the transient is not always
there. Frames are examined and dropped until the input looks like audio, so a warm device
costs nothing at all and a cold one costs about as long as it actually needs. The wait is
capped: an input that never settles is passed through with `gave_up` set, because silently
swallowing a user's microphone forever is a worse failure than a bad first second.

**Nothing is buffered.** Transient frames are measured and dropped, not held for later, so
this adds nothing to what `docs/RETENTION_POLICY.md` has to account for — the same property
`LevelWatchingSource` has, for the same reason.
"""

from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Iterator

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.levels import CLIPPED_SAMPLE, FULL_SCALE
from on_the_fly.domain.audio.ports import AudioSource

# A settled input on this hardware holds |DC| under 0.045 of full scale over 100 ms of room
# noise; the transient is at 1.0 and passes 0.13 as late as 1300 ms. The threshold sits
# above the first and far below the second.
SETTLED_DC = 0.05

# Clipping during settling is the rail, not a loud noise: 100% of samples, not a few. A
# settled window on the same machine measures at most 1%, so that is where the line goes.
SETTLED_CLIPPED_FRACTION = 0.01

# Long enough that the verdict is about the input rather than one unlucky frame, short
# enough that a warm device is released almost immediately. Twelve frames at the 20 ms
# the pipeline uses everywhere, so the cost of settling a clean device is a round number.
DEFAULT_WINDOW_MS = 240

# Cold starts here settle by about 1.8 s. Three seconds leaves room for slower hardware and
# still bounds how long a user can be left with no audio and no explanation.
DEFAULT_MAX_SETTLE_MS = 3_000


class SettlingSource:
    """An `AudioSource` that drops frames until the input stops being a power-up transient.

    Wraps a microphone, not a file: a recording has no analog path to power up, and dropping
    the first second of one would be destroying the user's data rather than a transient.
    """

    __slots__ = (
        "_dropped_bytes",
        "_gave_up",
        "_max_settle_ms",
        "_settled",
        "_source",
        "_window",
        "_window_ms",
    )

    def __init__(
        self,
        source: AudioSource,
        *,
        window_ms: int = DEFAULT_WINDOW_MS,
        max_settle_ms: int = DEFAULT_MAX_SETTLE_MS,
    ) -> None:
        if window_ms <= 0:
            raise ValueError(f"the window must be positive, got {window_ms}ms")
        if max_settle_ms < window_ms:
            raise ValueError(
                f"the settle cap ({max_settle_ms}ms) must be at least one window ({window_ms}ms), "
                "or the input is released before it has been looked at"
            )
        self._source = source
        self._window_ms = window_ms
        self._max_settle_ms = max_settle_ms
        # (dc sum, clipped samples, sample count) per frame. Counters, never audio.
        self._window: deque[tuple[float, int, int]] = deque()
        self._dropped_bytes = 0
        self._settled = False
        self._gave_up = False

    @property
    def audio_format(self) -> AudioFormat:
        return self._source.audio_format

    @property
    def source(self) -> AudioSource:
        """The wrapped source, for callers needing something this port does not expose."""
        return self._source

    @property
    def is_settled(self) -> bool:
        """Whether audio is being passed through yet."""
        return self._settled

    @property
    def gave_up(self) -> bool:
        """True when the cap expired and unsettled audio is being passed through anyway."""
        return self._gave_up

    @property
    def discarded_ms(self) -> int:
        """How much audio was dropped. `OPERATIONAL_METADATA`: a duration, not a recording."""
        return round(self._source.audio_format.duration_seconds(self._dropped_bytes) * 1000)

    def frames(self) -> Iterator[bytes]:
        for frame in self._source.frames():
            if self._settled:
                yield frame
                continue
            self._observe(frame)
            self._dropped_bytes += len(frame)
            if self.discarded_ms >= self._max_settle_ms:
                # Released rather than withheld. A microphone this project refuses to pass
                # through is indistinguishable, to the person holding it, from a broken one.
                self._settled = True
                self._gave_up = True
            elif self._is_window_settled():
                self._settled = True

    def close(self) -> None:
        self._source.close()

    # -- internals -----------------------------------------------------------------------

    def _observe(self, frame: bytes) -> None:
        """Record one frame's DC and clipping, and drop what has aged out of the window."""
        if len(frame) % 2 != 0:
            raise ValueError(f"frame of {len(frame)} bytes is not a whole number of 16-bit samples")
        samples = array("h")
        samples.frombytes(frame)
        total = 0.0
        clipped = 0
        for sample in samples:
            total += sample
            if sample >= CLIPPED_SAMPLE or sample <= -CLIPPED_SAMPLE:
                clipped += 1
        self._window.append((total, clipped, len(samples)))

        held = sum(entry[2] for entry in self._window)
        wanted = self._window_samples()
        while len(self._window) > 1 and held - self._window[0][2] >= wanted:
            held -= self._window.popleft()[2]

    def _window_samples(self) -> int:
        fmt = self._source.audio_format
        return fmt.sample_rate_hz * self._window_ms // 1000

    def _is_window_settled(self) -> bool:
        """A full window of centred, unclipped audio means the analog path has come up."""
        samples = sum(entry[2] for entry in self._window)
        if samples < self._window_samples():
            # Not enough audio to judge on. Deciding from a fraction of a window is how a
            # single quiet frame inside the transient would release it early.
            return False
        dc = abs(sum(entry[0] for entry in self._window) / samples) / FULL_SCALE
        clipped = sum(entry[1] for entry in self._window) / samples
        return dc < SETTLED_DC and clipped < SETTLED_CLIPPED_FRACTION
