"""Audio capture, voice activity detection, and utterance segmentation.

The first half of the translation pipeline: raw frames in, complete utterances out, with
the retention rule applied from the first frame rather than bolted on afterwards.

```text
AudioSource (adapter)
      │  frames
      ▼
VoiceActivityDetector ──► UtteranceSegmenter ──► EphemeralStore
      │                         │                     │
   per frame            pre-roll ring,          10s post-use,
                        bounded buffer          clock-driven deletion
                                │
                                ▼
                          Utterance (a handle plus metadata, never content)
```

Nothing here imports a UI framework, a device library, or a model runtime. `ports.py` is
the seam; phase 2 replaces implementations behind it without touching this pipeline
(ADR 0002).

Wiring it up:

```python
store = EphemeralStore("on-the-fly")
session = CaptureSession(source=microphone, detector=EnergyVoiceActivityDetector(), store=store)

with ThreadedReaper(store), session:
    for utterance in session.utterances():
        with store.borrow(utterance.handle) as audio:
            text = recognizer.transcribe(audio, utterance.audio_format)
        # the window on that audio restarts here and runs out ten seconds later
```

`SpeechRecognizer` and `Translator` are declared in `ports.py` and deliberately have no
implementations: adopting a model is a dependency-admission decision under Article 12, and
model weights are executable trust like any other dependency.
"""

from __future__ import annotations

from on_the_fly.domain.audio.formats import (
    RECOMMENDED_SAMPLE_RATE_HZ,
    SUPPORTED_SAMPLE_WIDTH_BYTES,
    AudioFormat,
)
from on_the_fly.domain.audio.ports import (
    AudioSource,
    SpeechRecognizer,
    Translator,
    VoiceActivityDetector,
)
from on_the_fly.domain.audio.segmenter import (
    ABSOLUTE_MAX_UTTERANCE_MS,
    DEFAULT_HANGOVER_MS,
    DEFAULT_MAX_UTTERANCE_MS,
    DEFAULT_MIN_UTTERANCE_MS,
    DEFAULT_PRE_ROLL_MS,
    EndReason,
    SegmenterConfig,
    Utterance,
    UtteranceSegmenter,
)
from on_the_fly.domain.audio.session import (
    CaptureError,
    CaptureSession,
    CaptureStats,
)
from on_the_fly.domain.audio.streaming import (
    BatchStreamingRecognizer,
    StreamingRecognizer,
    TranscriptEvent,
)
from on_the_fly.domain.audio.vad import (
    DEFAULT_ABSOLUTE_SILENCE_RMS,
    DEFAULT_ADAPTATION_RATE,
    DEFAULT_SPEECH_FACTOR,
    EnergyVoiceActivityDetector,
    frame_rms,
)

__all__ = [
    "ABSOLUTE_MAX_UTTERANCE_MS",
    "DEFAULT_ABSOLUTE_SILENCE_RMS",
    "DEFAULT_ADAPTATION_RATE",
    "DEFAULT_HANGOVER_MS",
    "DEFAULT_MAX_UTTERANCE_MS",
    "DEFAULT_MIN_UTTERANCE_MS",
    "DEFAULT_PRE_ROLL_MS",
    "DEFAULT_SPEECH_FACTOR",
    "RECOMMENDED_SAMPLE_RATE_HZ",
    "SUPPORTED_SAMPLE_WIDTH_BYTES",
    "AudioFormat",
    "AudioSource",
    "BatchStreamingRecognizer",
    "CaptureError",
    "CaptureSession",
    "CaptureStats",
    "EndReason",
    "EnergyVoiceActivityDetector",
    "SegmenterConfig",
    "SpeechRecognizer",
    "StreamingRecognizer",
    "TranscriptEvent",
    "Translator",
    "Utterance",
    "UtteranceSegmenter",
    "VoiceActivityDetector",
    "frame_rms",
]
