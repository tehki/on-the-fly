"""Translation infrastructure: OPUS-MT weights on two engines (ADR 0009, ADR 0018).

The only place in this project that knows a translation engine exists. Above this package,
translation is the `Translator` port in `domain/audio/ports.py` and nothing more.

```text
MarianArtifact          a pinned publisher artefact: URL, digest, licence
TranslationModelStore   fetch -> verify -> extract -> convert; never returns unverified
OpusMtTranslator        the Translator port on CTranslate2 — faster, desktop default
OnnxTranslationModel    a pinned ONNX export: Hugging Face revision plus file digests
OnnxTranslator          the Translator port on ONNX Runtime — the engine phones can run
resolve_engine          which artefact serves a pair on an engine, without fetching it
open_translator         fetch, verify and load one; the only place the engines differ
```

**Two implementations, one port, and callers choose by engine rather than by class.**
CTranslate2 is measurably faster and stays the default; ONNX Runtime exists because
CTranslate2 has no Android or iOS build and this project has to run on a phone (ADR 0017).

Two rules from ADR 0009 are enforced in code here rather than left to a reader:
the pin covers the publisher's archive and never the conversion derived from it, and
nothing is cached between calls, because a translation cache retains project content past
its retention window.
"""

from on_the_fly.infrastructure.translation.artifacts import (
    KNOWN_ARTIFACTS,
    OPUS_MT_EN_RU,
    OPUS_MT_RU_EN,
    ArtifactIntegrityError,
    ArtifactNotPresentError,
    MarianArtifact,
    TranslationArtifactError,
    TranslationModelStore,
    resolve,
)
from on_the_fly.infrastructure.translation.engines import (
    DEFAULT_ENGINE,
    TranslationChoice,
    TranslationEngine,
    open_translator,
)
from on_the_fly.infrastructure.translation.engines import resolve as resolve_engine
from on_the_fly.infrastructure.translation.onnx_artifacts import (
    KNOWN_ONNX_MODELS,
    ONNX_OPUS_MT_EN_RU,
    ONNX_OPUS_MT_RU_EN,
    OnnxTranslationModel,
    resolve_onnx,
)
from on_the_fly.infrastructure.translation.onnx_translator import OnnxTranslator
from on_the_fly.infrastructure.translation.opus_mt import (
    OpusMtTranslator,
    TranslationError,
    UnsupportedPairError,
    load,
    sentence_case,
)

__all__ = [
    "DEFAULT_ENGINE",
    "KNOWN_ARTIFACTS",
    "KNOWN_ONNX_MODELS",
    "ONNX_OPUS_MT_EN_RU",
    "ONNX_OPUS_MT_RU_EN",
    "OPUS_MT_EN_RU",
    "OPUS_MT_RU_EN",
    "ArtifactIntegrityError",
    "ArtifactNotPresentError",
    "MarianArtifact",
    "OnnxTranslationModel",
    "OnnxTranslator",
    "OpusMtTranslator",
    "TranslationArtifactError",
    "TranslationChoice",
    "TranslationEngine",
    "TranslationError",
    "TranslationModelStore",
    "UnsupportedPairError",
    "load",
    "open_translator",
    "resolve",
    "resolve_engine",
    "resolve_onnx",
    "sentence_case",
]
