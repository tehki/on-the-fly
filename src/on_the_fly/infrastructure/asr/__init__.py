"""Speech recognition infrastructure: which models this project will recognise with.

The only place Whisper or sherpa-onnx appear. `domain/audio/ports.py` declares
`SpeechRecognizer`; nothing above this package learns which one it got.

```python
from on_the_fly.infrastructure.model_store import ModelStore

store = ModelStore(cache_dir, allow_download=True)
recognizer = FasterWhisperRecognizer(store.ensure(DEFAULT_MODEL))
```

`ModelStore` used to live in this package and no longer does. Nothing in it was ever
speech-specific — it pins a Hugging Face revision and checks digests — and by the time four
of the seven pinned artefacts were translation models, a translator reaching into `asr/` to
verify its own weights had stopped making sense. It is `infrastructure/model_store.py`, and
this package imports it like every other caller rather than re-exporting it.
"""

from __future__ import annotations

from on_the_fly.infrastructure.asr.models import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    STREAMING_EN,
    STREAMING_LAYOUTS,
    TINY,
    resolve,
)
from on_the_fly.infrastructure.asr.sherpa_streaming import (
    SherpaStreamingRecognizer,
    StreamingRecognitionError,
)
from on_the_fly.infrastructure.asr.whisper_recognizer import (
    REQUIRED_SAMPLE_RATE_HZ,
    FasterWhisperRecognizer,
    RecognitionError,
)

__all__ = [
    "DEFAULT_MODEL",
    "KNOWN_MODELS",
    "REQUIRED_SAMPLE_RATE_HZ",
    "STREAMING_EN",
    "STREAMING_LAYOUTS",
    "TINY",
    "FasterWhisperRecognizer",
    "RecognitionError",
    "SherpaStreamingRecognizer",
    "StreamingRecognitionError",
    "resolve",
]
