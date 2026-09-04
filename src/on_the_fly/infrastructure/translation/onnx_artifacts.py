"""Which ONNX translation models may be loaded, and whose conversion they are (ADR 0018).

ADR 0009 set the rule that governs this file: **pin what the publisher published, never a
conversion.** The CTranslate2 route obeys it by pinning Helsinki-NLP's own `.zip` and
treating the converted directory as a derived cache, verified against nothing because no
publisher's digest exists for it.

That route is not available here, and the honest thing is to say why rather than to present
this as the same arrangement. Helsinki-NLP publishes Marian weights; they do not publish an
ONNX export. So there are two ways to obtain one, and ADR 0009 anticipated both:

1. **Export it here.** The export tool is `optimum`, which requires `transformers` and
   `torch` — the multi-gigabyte dependency ADR 0005 declined and the one thing that cannot
   ship to a phone. It would also make this project the publisher of the artefact, with the
   obligations that carries, and a digest over a locally exported file attests to the
   machine that exported it and nothing else (ADR 0007's objection, which still stands).
2. **Pin a third-party conversion, and admit that publisher under Article 12 with the same
   scrutiny any dependency gets.** That is what is done below.

## Admission review — `onnx-community` (Article 12)

| Criterion | Finding |
| --- | --- |
| What it is | The Hugging Face organisation publishing ONNX exports for `transformers.js` |
| Declared licence | **cc-by-4.0** — the licence that travels inside Helsinki-NLP's own archive |
| Base model | `Helsinki-NLP/opus-mt-en-ru`, declared in the repository's own metadata |
| Pinned to | Revision `c6967b32`, plus a SHA-256 for every file loaded |
| Executes anything at load | No. `onnxruntime` reads a graph; there is no code path in the repo |

The licence line is the strongest signal available and worth stating precisely: the ONNX
export declares **cc-by-4.0**, which agrees with the `LICENSE` file inside the publisher's
own `.zip` and disagrees with the `apache-2.0` the Hugging Face mirror of the same model
claims (`artifacts.py`). A converter who tightened the declared licence to match the
upstream artefact rather than loosening it to match the convenient mirror is a converter
paying attention. `Xenova/opus-mt-en-ru`, the other export of this model, declares no
licence at all, and was not taken for that reason.

**What the review cannot establish is that the weights are Helsinki-NLP's.** No published
digest connects this export to the Marian archive, and the two formats are not comparable
byte for byte. The check that is actually available is behavioural, and it was run: on the
publisher's own 300-sentence test set this export scores within noise of the CTranslate2
conversion of the archive, agreeing with it exactly on most sentences (ADR 0018). That is
evidence of the same weights, not proof, and it is recorded as the former.

**Quantised int8, matching the CTranslate2 side.** Measured rather than assumed, because
"int8 is close enough" is the kind of claim this project keeps having to retract: on the
publisher's 300-sentence test set the full-precision graphs score **66.34 chrF2 against the
quantised 66.33** — no difference at all — for 653 MB against 421 MB. The 0.29 that
separates this export from the CTranslate2 conversion is therefore *not* quantisation; it is
the export, and it does not shrink by paying 232 MB more.
"""

from __future__ import annotations

from dataclasses import dataclass

from on_the_fly.infrastructure.asr.model_store import ModelPin
from on_the_fly.infrastructure.translation.artifacts import TranslationArtifactError

# `ModelPin`/`ModelStore` live under `asr/` for the historical reason that speech models
# needed pinning first. Nothing in them is speech-specific — they pin a Hugging Face
# revision and check digests — so they are reused here rather than duplicated. Moving them
# to `infrastructure/` proper is the correct end state and is deliberately not bundled into
# this change (ADR 0018, review trigger).

# The three graphs plus the tokenisers and the config the decoding loop reads. Nothing else
# from the repository is fetched: the merged decoder is unused (see `onnx_translator`), and
# the full-precision variants cost 232 MB more for a measured quality difference of 0.01
# chrF2 — which is to say, none.
#
# 421 MB, against 84 MB for the CTranslate2 conversion of the same model. Two thirds of that
# is the decoder weights, carried once in `decoder_model` and again in
# `decoder_with_past_model`. The merged graph exists to avoid exactly that and is broken on
# its no-cache path (ADR 0018), so the duplication is a cost this project pays knowingly and
# a real one on a phone.
_EN_RU_FILES = {
    "onnx/encoder_model_int8.onnx": (
        "b8b4f72528c0da92e579af8a739f97fa2f792d73527ba2fee786fa7286c4055b"
    ),
    "onnx/decoder_model_int8.onnx": (
        "cb095b6f25ac8671699600626b8b69684c9777d383c4ac81d0fc3a1d6610b496"
    ),
    "onnx/decoder_with_past_model_int8.onnx": (
        "c79dee26dff975cd55ea61272b2f273cbdd82300ad6a2a9fdbebb92766ec4efa"
    ),
    "source.spm": "16bebef1389a0b8ab452772c4e35b9e605e5713f8ac7baa71ca701394eaa086d",
    "target.spm": "745998e51ba5b058e38b7ac7765c25c43ed5c1c39cc92b27163b9b2e323c9d7c",
    "vocab.json": "5cf0d95d930d8d3e783c9e2f46a72f08b43a18060dab4ddefbcb66a733efedcb",
    "config.json": "8da686d7c49cc97f4c11ca17f1b07f9cc859b96e8aec880871d562ebbda458ed",
}


@dataclass(frozen=True)
class OnnxTranslationModel:
    """A pinned ONNX export serving one direction.

    Carries the attribution alongside the pin because CC-BY-4.0 obliges a notice a user can
    reach, and an attribution stored anywhere but next to the artefact it describes is an
    attribution that eventually describes the wrong artefact.
    """

    name: str
    pin: ModelPin
    source_language: str
    target_language: str
    licence: str
    attribution: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.source_language, self.target_language)

    def __str__(self) -> str:
        return f"{self.name} ({self.source_language}->{self.target_language}, {self.licence})"


ONNX_OPUS_MT_EN_RU = OnnxTranslationModel(
    name="onnx-opus-mt-en-ru",
    pin=ModelPin(
        name="onnx-opus-mt-en-ru",
        repo_id="onnx-community/opus-mt-en-ru",
        revision="c6967b328d292f9f3d14bab6e9de40f9bd367114",
        licence="CC-BY-4.0",
        digests=_EN_RU_FILES,
    ),
    source_language="en",
    target_language="ru",
    licence="CC-BY-4.0",
    # Both parties are named. The weights are Helsinki-NLP's work and the licence is
    # theirs; the export is someone else's, and a user reading the notice is entitled to
    # know which artefact is actually running.
    attribution=(
        "English-Russian translation by OPUS-MT (Helsinki-NLP), model opus-mt-en-ru, "
        "licensed CC-BY-4.0. ONNX conversion by onnx-community. "
        "https://github.com/Helsinki-NLP/Opus-MT"
    ),
)

KNOWN_ONNX_MODELS: dict[str, OnnxTranslationModel] = {
    ONNX_OPUS_MT_EN_RU.name: ONNX_OPUS_MT_EN_RU,
}


def resolve_onnx(pair: tuple[str, str]) -> OnnxTranslationModel:
    """Find the ONNX model serving a language pair, or refuse.

    Refusing rather than falling back to the CTranslate2 route is deliberate: a caller who
    asked for the mobile-capable engine and silently got the desktop one would be told the
    application runs on their phone when it does not.
    """
    for model in KNOWN_ONNX_MODELS.values():
        if model.pair == pair:
            return model
    known = ", ".join(
        f"{m.source_language}->{m.target_language}" for m in KNOWN_ONNX_MODELS.values()
    )
    raise TranslationArtifactError(
        f"no pinned ONNX translation model for {pair[0]}->{pair[1]}; this project has: {known}. "
        "Adding one means pinning a published export and recording whose conversion it is."
    )
