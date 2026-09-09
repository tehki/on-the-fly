"""The models this project is willing to load.

Every entry was produced by `scripts/pin_model.py` and landed in a reviewed commit. Adding
a model is therefore a deliberate act with a diff, not a runtime string somebody passed in
— which is the point, because `ModelStore` refuses anything it cannot verify.

Whisper weights are MIT (OpenAI), as are the CTranslate2 conversions published by Systran.
That matters: several of the strongest multilingual models are non-commercial, and this
repository is Apache-2.0 (ADR 0001).
"""

from __future__ import annotations

from on_the_fly.infrastructure.asr.sherpa_streaming import ENGLISH_LAYOUT, StreamingLayout
from on_the_fly.infrastructure.model_store import ModelPin

# 78.2 MB. The smallest useful Whisper model: fast enough to prove the pipeline on a CPU.
#
# "Not accurate enough to ship a translator on" is what this comment used to say, unmeasured
# since ADR 0005. Measured in ADR 0035 it is wrong about English and right about everything
# else: on clean read English it scores about 1.5% word error once orthography is discounted,
# and on clean read French 77.1% — where the pinned French streaming model scores 14.3% on
# the same clips. It does not return a flawed transcript in French; it returns a different
# sentence.
#
# Larger models are added by running scripts/pin_model.py and committing the result.
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

# The two larger Whisper models, pinned so the sentence above can be tested rather than
# repeated. ADR 0035 measured `tiny` and said plainly what it could not say: "a larger Whisper
# would very likely score far better and would be a different pin, a different download and a
# different latency decision". These are that pin; ADR 0041 is that measurement.
#
# Same publisher, same licence, same four files, same tokeniser and vocabulary byte for byte —
# `tokenizer.json` and `vocabulary.txt` have identical digests across all three sizes, which is
# what one model family in three sizes looks like from outside.
BASE = ModelPin(
    name="base",
    repo_id="Systran/faster-whisper-base",
    revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    licence="MIT",
    digests={
        "config.json": "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a",
        "model.bin": "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
        "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
        "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    },
)

SMALL = ModelPin(
    name="small",
    repo_id="Systran/faster-whisper-small",
    revision="536b0662742c02347bc0e980a01041f333bce120",
    licence="MIT",
    digests={
        "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
        "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
        "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    },
)

# The chunk/left-context variant of the pinned English model. Named once so the digest
# entries stay readable rather than wrapping mid-hash.
_EN_VARIANT = "epoch-99-avg-1-chunk-16-left-64.int8.onnx"

# The same, for French. No chunk size in the name: a different icefall recipe.
_FR_VARIANT = "epoch-29-avg-9-with-averaged-model.int8.onnx"

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

# Streaming Russian (ADR 0012). Apache-2.0, first-party, 95 MB across three chunk64 files.
#
# ADR 0011 concluded that no licence-clean streaming Russian model existed. It was wrong:
# it checked `alphacep/vosk-model-ru` and `alphacep/vosk-model-small-ru` and stopped, and
# the model it needed is a third repository whose name says what it is. The sherpa-onnx
# republication's README names it in one line, which is exactly where its maintainer
# pointed when asked.
#
# `chunk64` rather than int8: those are the variants exported with the streaming metadata
# sherpa requires. The encoder's own metadata says `streaming zipformer2` and carries the
# `encoder_dims` the other two lacked — which is the difference the earlier ADR read as a
# licensing problem when it was a packaging one.
STREAMING_RU = ModelPin(
    name="streaming-ru",
    repo_id="alphacep/vosk-model-small-streaming-ru",
    revision="e18123ee13f694036a1eea82eb43f9895387cb59",
    licence="Apache-2.0",
    digests={
        "am-onnx/encoder.chunk64.onnx": (
            "5423647f6fc579c765c494ef4f6747c3cfc1847d08691cceac7b6b4210620982"
        ),
        "am-onnx/decoder.chunk64.onnx": (
            "3cca47e861640eed6b0693fd68fa25a48ed584ab053e0db8259fa26cbf85054e"
        ),
        "am-onnx/joiner.chunk64.onnx": (
            "df4cd0d4609a5877a0b72a44c439b5baefd1788249cb59327dc3cf476ef34219"
        ),
        "lang/tokens.txt": "93bbbc0bae6b78c0bbb743d4aa9fded3bb5ff3aac5f0200e3a769a5a05e0fdf6",
    },
)

# Streaming French (ADR 0031). Apache-2.0, 128.2 MB across three int8 files.
#
# A third publisher, and the first model here that is a republication of somebody else's
# training run: `shaojieli` exported it to ONNX from their own icefall recipe trained on
# Common Voice French, and the model card names that source repository. Both declare
# Apache-2.0.
#
# The file names carry no chunk size, because this is the `pruned_transducer_stateless7_
# streaming` recipe rather than the chunked exports the English and Russian pins use. The
# streaming geometry is baked into the graph instead of selected by filename.
STREAMING_FR = ModelPin(
    name="streaming-fr",
    repo_id="shaojieli/sherpa-onnx-streaming-zipformer-fr-2023-04-14",
    revision="3db9565d9633758d6b87b9a7b3dc09ebfb6b2c73",
    licence="Apache-2.0",
    digests={
        f"decoder-{_FR_VARIANT}": (
            "e72b2b9ed36355bd0dd43433f7dd258e7226ab54c9ef42b28c73ebb785805623"
        ),
        f"encoder-{_FR_VARIANT}": (
            "47a94a7fdc8dff63d708be4ea0535747640224467f91e238311f1ddbdd09327e"
        ),
        f"joiner-{_FR_VARIANT}": (
            "fc2f3bb851a15a532c6f2422d53eecd1ca949f12b0897e07a852021c30481711"
        ),
        "tokens.txt": "37fb3f2a7bcb85e5fff3f1f66be04e6fbb05077a22f56d177fe85704e945fb31",
    },
)

# Which file plays which role, per pin. The English model names its files after a training
# epoch and the Russian one after a chunk size; neither is a convention worth guessing at.
STREAMING_LAYOUTS: dict[str, StreamingLayout] = {
    STREAMING_EN.name: ENGLISH_LAYOUT,
    STREAMING_RU.name: StreamingLayout(
        encoder="am-onnx/encoder.chunk64.onnx",
        decoder="am-onnx/decoder.chunk64.onnx",
        joiner="am-onnx/joiner.chunk64.onnx",
        tokens="lang/tokens.txt",
    ),
    STREAMING_FR.name: StreamingLayout(
        encoder=f"encoder-{_FR_VARIANT}",
        decoder=f"decoder-{_FR_VARIANT}",
        joiner=f"joiner-{_FR_VARIANT}",
    ),
}

# Every streaming pin is named `streaming-<language code>`, and `app/catalogue.py` builds
# the name from a language code with this. One naming rule, in one place.
STREAMING_PIN_PREFIX = "streaming-"

KNOWN_MODELS: dict[str, ModelPin] = {
    TINY.name: TINY,
    BASE.name: BASE,
    SMALL.name: SMALL,
    STREAMING_EN.name: STREAMING_EN,
    STREAMING_RU.name: STREAMING_RU,
    STREAMING_FR.name: STREAMING_FR,
}

# `base` and not `tiny` since ADR 0041. Measured on both published test sets this project has
# references for, `base` is better than `tiny` everywhere it was measured and — on French —
# also *faster*, because a model that returns a different sentence spends longer returning it.
DEFAULT_MODEL = BASE


def streaming_pins() -> dict[str, ModelPin]:
    """The pins that stream, keyed by name.

    Derived from the registry rather than listed, so a pin and the things that must agree
    with it cannot fall out of step by one of them being edited.
    """
    return {
        name: pin for name, pin in KNOWN_MODELS.items() if name.startswith(STREAMING_PIN_PREFIX)
    }


def batch_pins() -> dict[str, ModelPin]:
    """The pins the batch recogniser can load, keyed by name.

    The complement of `streaming_pins`, from the same one rule, because the two lists have to
    partition the registry and a hand-maintained pair of them would not.

    This exists because `--model` offered every pin in the registry, including the streaming
    ones. `transcribe --model streaming-en` was an accepted argument that verified a 73 MB
    model and then failed with `Unable to open file 'model.bin'` — a picker promising
    something it cannot serve, which is the defect ADR 0034 removed from the window's language
    pickers and left standing here.
    """
    return {name: pin for name, pin in KNOWN_MODELS.items() if name not in streaming_pins()}


def layout_for(pin: ModelPin) -> StreamingLayout:
    """Which file in `pin`'s directory plays which role, or refuse.

    Two registries have to agree here, and only one of them was ever asked. A pin can be
    added to `KNOWN_MODELS` — and a language moved to the streaming tier alongside it —
    without anyone touching `STREAMING_LAYOUTS`, which is a separate dict further up the
    same file with no cue to update it. `app/catalogue.py` would then report the language
    as recognisable, an interface would offer it, and the three call sites that index the
    layouts by pin name would raise a bare `KeyError` at the moment the user pressed
    Listen.

    That is the drift `catalogue.py` was written to refuse, on the one leg it did not know
    about. Refusing here, in the shape `resolve()` already refuses an unknown model, gives
    the catalogue something to ask and the user a sentence instead of a traceback.
    """
    try:
        return STREAMING_LAYOUTS[pin.name]
    except KeyError:
        known = ", ".join(sorted(STREAMING_LAYOUTS))
        raise KeyError(
            f"model {pin.name!r} is pinned but no file layout is recorded for it; "
            f"layouts exist for: {known}. Each publisher names the encoder, decoder and "
            "joiner differently, so there is nothing to guess — add a StreamingLayout in "
            "the same commit as the pin."
        ) from None


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
