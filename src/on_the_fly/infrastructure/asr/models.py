"""The models this project is willing to load.

Every entry was produced by `scripts/pin_model.py` and landed in a reviewed commit. Adding
a model is therefore a deliberate act with a diff, not a runtime string somebody passed in
— which is the point, because `ModelStore` refuses anything it cannot verify.

Whisper weights are MIT (OpenAI), as are the CTranslate2 conversions published by Systran.
That matters: several of the strongest multilingual models are non-commercial, and this
repository is Apache-2.0 (ADR 0001).
"""

from __future__ import annotations

from on_the_fly.infrastructure.asr.model_store import ModelPin

# 78.2 MB. The smallest useful Whisper model: fast enough to prove the pipeline on a CPU,
# and honestly not accurate enough to ship a translator on. Larger models are added by
# running scripts/pin_model.py and committing the result.
TINY = ModelPin(
    name="tiny",
    repo_id="Systran/faster-whisper-tiny",
    revision="d90ca5fe260221311c53c58e660288d3deb8d356",
    licence="MIT",
    digests={
        "config.json": "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8",
        "model.bin": "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
        "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
        "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    },
)

# The chunk/left-context variant of the pinned English model. Named once so the digest
# entries stay readable rather than wrapping mid-hash.
_EN_VARIANT = "epoch-99-avg-1-chunk-16-left-64.int8.onnx"

# Streaming English (ADR 0006, ADR 0008). Apache-2.0, 72.7 MB. The int8 chunk-16-left-64
# variant: the smaller left context is the lower-latency one, which is the whole point.
STREAMING_EN = ModelPin(
    name="streaming-en",
    repo_id="csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26",
    revision="672fbf1b30579d6585301139bb363f42a0ad4a24",
    licence="Apache-2.0",
    digests={
        f"decoder-{_EN_VARIANT}": (
            "98da299f471e38bb4e1a8df579b8cc9122d6039576a77e357b3c60f17dd83b02"
        ),
        f"encoder-{_EN_VARIANT}": (
            "0d072fd4ef956294ba9db9e9a71a541ac70659095ec4934c8453d8b2fe740187"
        ),
        f"joiner-{_EN_VARIANT}": (
            "d944208d660d67c8d72cd2acaeac971fa5ceb8c80e76c1968148846fedd6e297"
        ),
        "tokens.txt": "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
    },
)

KNOWN_MODELS: dict[str, ModelPin] = {TINY.name: TINY, STREAMING_EN.name: STREAMING_EN}

DEFAULT_MODEL = TINY


def resolve(name: str) -> ModelPin:
    """Look up a pinned model by name, or refuse.

    An unknown name is refused rather than passed through to the model hub. Accepting an
    arbitrary repository here would let a caller reach any weights on the internet, which
    is precisely the trust decision the pin registry exists to make.
    """
    try:
        return KNOWN_MODELS[name]
    except KeyError:
        known = ", ".join(sorted(KNOWN_MODELS))
        raise KeyError(
            f"unknown model {name!r}; pinned models are: {known}. "
            "Add one with scripts/pin_model.py and commit the pin."
        ) from None
