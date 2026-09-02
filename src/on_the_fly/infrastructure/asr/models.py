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

KNOWN_MODELS: dict[str, ModelPin] = {TINY.name: TINY}

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
