"""Speech recognition infrastructure: pinned models and the recogniser that loads them.

The only place Whisper, CTranslate2 or the model hub appear. `domain/audio/ports.py`
declares `SpeechRecognizer`; nothing above this package learns which one it got.

```python
store = ModelStore(cache_dir, allow_download=True)
recognizer = FasterWhisperRecognizer(store.ensure(DEFAULT_MODEL))
```

`ModelStore.ensure` returns a directory whose every file matched a digest committed to this
repository, or it raises. There is no path through it that yields an unverified model.
"""

from __future__ import annotations

from on_the_fly.infrastructure.asr.model_store import (
    ModelIntegrityError,
    ModelNotPresentError,
    ModelPin,
    ModelStore,
    ModelStoreError,
    compute_digests,
    file_digest,
)
from on_the_fly.infrastructure.asr.models import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    STREAMING_EN,
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
    "TINY",
    "FasterWhisperRecognizer",
    "ModelIntegrityError",
    "ModelNotPresentError",
    "ModelPin",
    "ModelStore",
    "ModelStoreError",
    "RecognitionError",
    "SherpaStreamingRecognizer",
    "StreamingRecognitionError",
    "compute_digests",
    "file_digest",
    "resolve",
]
