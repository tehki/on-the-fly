"""Translation infrastructure: CTranslate2 running OPUS-MT weights (ADR 0009).

The only place in this project that knows a translation engine exists. Above this package,
translation is the `Translator` port in `domain/audio/ports.py` and nothing more.

```text
MarianArtifact          a pinned publisher artefact: URL, digest, licence
TranslationModelStore   fetch -> verify -> extract -> convert; never returns unverified
OpusMtTranslator        the Translator port, for exactly one direction
```

Two rules from ADR 0009 are enforced in code here rather than left to a reader:
the pin covers the publisher's archive and never the conversion derived from it, and
nothing is cached between calls, because a translation cache retains project content past
its retention window.
"""

from on_the_fly.infrastructure.translation.artifacts import (
    KNOWN_ARTIFACTS,
    OPUS_MT_EN_RU,
    ArtifactIntegrityError,
    ArtifactNotPresentError,
    MarianArtifact,
    TranslationArtifactError,
    TranslationModelStore,
    resolve,
)
from on_the_fly.infrastructure.translation.opus_mt import (
    OpusMtTranslator,
    TranslationError,
    UnsupportedPairError,
    load,
    sentence_case,
)

__all__ = [
    "KNOWN_ARTIFACTS",
    "OPUS_MT_EN_RU",
    "ArtifactIntegrityError",
    "ArtifactNotPresentError",
    "MarianArtifact",
    "OpusMtTranslator",
    "TranslationArtifactError",
    "TranslationError",
    "TranslationModelStore",
    "UnsupportedPairError",
    "load",
    "resolve",
    "sentence_case",
]
