"""Audio infrastructure: the adapters that connect the pipeline to real hardware.

This is the only place in the project that knows PortAudio exists. `domain/audio/` defines
`AudioSource` and never learns what implements it, so phase 2 replaces this package with a
platform-native one and the pipeline above is untouched (ADR 0002).

```python
from on_the_fly.domain.audio import CaptureSession, EnergyVoiceActivityDetector
from on_the_fly.domain.retention import EphemeralStore, ThreadedReaper
from on_the_fly.infrastructure.audio import MicrophoneSource

store = EphemeralStore("on-the-fly")
session = CaptureSession(
    source=MicrophoneSource(),
    detector=EnergyVoiceActivityDetector(),
    store=store,
)

with ThreadedReaper(store), session:
    for utterance in session.utterances():
        with store.borrow(utterance.handle) as audio:
            ...  # recognise, translate
```

Constructing a `MicrophoneSource` opens nothing. The device is acquired when capture
starts and released deterministically when it ends.
"""

from __future__ import annotations

from on_the_fly.infrastructure.audio.backend import (
    PCM_DTYPE,
    AudioDeviceError,
    CaptureBackend,
    InputStream,
    SoundDeviceBackend,
)
from on_the_fly.infrastructure.audio.microphone import DEFAULT_FRAME_MS, MicrophoneSource

__all__ = [
    "DEFAULT_FRAME_MS",
    "PCM_DTYPE",
    "AudioDeviceError",
    "CaptureBackend",
    "InputStream",
    "MicrophoneSource",
    "SoundDeviceBackend",
]
