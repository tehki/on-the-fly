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

from on_the_fly.infrastructure.model_store import ModelPin
from on_the_fly.infrastructure.translation.artifacts import TranslationArtifactError

# `ModelPin`/`ModelStore` are `infrastructure/model_store.py`. They lived under `asr/` for
# the historical reason that speech models needed pinning first, and ADR 0018 recorded
# moving them as the correct end state; that was done once half the artefacts `ModelStore`
# verifies were translation models — four of eight — and a translator reaching into `asr/` to
# verify its own weights had stopped making sense.

# The three graphs, the tokenisers, and both configs the decoding loop reads —
# `generation_config.json` included, because it carries the `bad_words_ids` the loop is
# required to honour and an unverified constraint is not a constraint. Nothing else
# from the repository is fetched: the merged decoder is unused (see `onnx_translator`), and
# the full-precision variants cost 232 MB more for a measured quality difference of 0.01
# chrF2 — which is to say, none.
#
# 421 MB, against 84 MB for the CTranslate2 conversion of the same model. Two thirds of that
# is the decoder weights, carried once in `decoder_model` and again in
# `decoder_with_past_model`. The merged graph would save 183 MB of it and **works** — it is
# twice as slow and 0.51 chrF2 worse, measured, so the duplication is bought deliberately
# rather than forced (ADR 0018). On a phone that trade is worth revisiting; latency is what
# decides it, and latency is already this engine's binding constraint.
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
    "generation_config.json": ("9996e913a167485b49c1afb315b5eda9270a9310c720699bf617757cc71c81a5"),
}


# The other direction. Two digests here are the same values that appear above with their
# roles swapped — this export's `source.spm` is the other's `target.spm` — because OPUS-MT
# trains the pair on one joint sentencepiece vocabulary. `vocab.json` is byte-identical in
# both. That is a free cross-check that these two repositories are the same model family
# rather than two unrelated exports that happen to share a naming convention.
_RU_EN_FILES = {
    "onnx/encoder_model_int8.onnx": (
        "fdd4d1de9cb02feaae8bc892e0e21b1bfd1741fa43a2895d471ee5f53ae260c4"
    ),
    "onnx/decoder_model_int8.onnx": (
        "29a0c34e0796d01eea5220ff8c4dbe1616f0cf7aa7dfc940de0ca82d3a93fcf3"
    ),
    "onnx/decoder_with_past_model_int8.onnx": (
        "f39675dcf799f72aa6e8d86c933787d16c330c7bfcf8da05e132455abd53f279"
    ),
    "source.spm": "745998e51ba5b058e38b7ac7765c25c43ed5c1c39cc92b27163b9b2e323c9d7c",
    "target.spm": "16bebef1389a0b8ab452772c4e35b9e605e5713f8ac7baa71ca701394eaa086d",
    "vocab.json": "5cf0d95d930d8d3e783c9e2f46a72f08b43a18060dab4ddefbcb66a733efedcb",
    "config.json": "bf674cad9714456e2ba2fa886c79dace4c884ba63ed5369f37866cff6164da23",
    "generation_config.json": ("b9aa8ac0671a1beebe5bf218deec080a9d18b1a56a16268416d5b38e494ddc19"),
}


# French, both directions (ADR 0033). Same publisher, same file set, same int8 choice as the
# Russian pair above.
#
# The joint-vocabulary cross-check the `_RU_EN_FILES` comment describes holds here too, and
# was used the same way: this export's `source.spm` is the other's `target.spm` and vice
# versa, `vocab.json` is byte-identical across the two repositories, and only `config.json`
# differs. Two unrelated exports that happened to share a naming convention would not do
# that.
#
# Their file *sizes* are identical to the byte across both directions — 50065734 for every
# encoder, 178814001 for every decoder. That is one architecture and one vocabulary size,
# not one artefact published twice: the digests differ, which is what was checked.
_EN_FR_FILES = {
    "onnx/encoder_model_int8.onnx": (
        "b6190be92972c9674abb94561dcffc12d89463269748be0db459974037319b79"
    ),
    "onnx/decoder_model_int8.onnx": (
        "84e1755a83fb34c110c7edf67ab95348cd2bcf942a1b6cb721bbfc6c7f520ffe"
    ),
    "onnx/decoder_with_past_model_int8.onnx": (
        "34f57a0bf86b0599cccdce0af5ba4458afbc7c7e32922e2b715852e1b11a522c"
    ),
    "source.spm": "173e9f493a668fe396d599e28d414a201193094e6ffd7a4678e5aab0f6d3d838",
    "target.spm": "78d0e717c77053f1c4b856d8661d9cb87c64f083a35418c087b9146300e4f585",
    "vocab.json": "f2ba9c69ae20f96b8bd821239a9152be422394f980350b77907cffc183db5f2d",
    "config.json": "5c3abfc0f9fce281e988cce9cd33e157c56ffbddf1271e98b0ee139d4e71942d",
    "generation_config.json": ("3517305631bcbf7c07429f5c6b85e9e7cbf96e607445cf38f627e6f447055e2f"),
}


_FR_EN_FILES = {
    "onnx/encoder_model_int8.onnx": (
        "df4c59dc69e422f504ccb7bf1895f656f20e16542135c1da1d7092b80b17e095"
    ),
    "onnx/decoder_model_int8.onnx": (
        "7dc2836a33957b5db646074806828301ab643a1adc2eabde6c3782cda503cdc9"
    ),
    "onnx/decoder_with_past_model_int8.onnx": (
        "5cd4ea62be08cdb257d9c52b82f743c5bebe7c04baabf25f19300296ca4bf370"
    ),
    "source.spm": "78d0e717c77053f1c4b856d8661d9cb87c64f083a35418c087b9146300e4f585",
    "target.spm": "173e9f493a668fe396d599e28d414a201193094e6ffd7a4678e5aab0f6d3d838",
    "vocab.json": "f2ba9c69ae20f96b8bd821239a9152be422394f980350b77907cffc183db5f2d",
    "config.json": "3dfa2f43c43a62be5f95ad61aac93faa139dcfab5d4527633e82df2677bb8d1d",
    "generation_config.json": ("3517305631bcbf7c07429f5c6b85e9e7cbf96e607445cf38f627e6f447055e2f"),
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

# Which Marian release this export descends from is checkable and was checked: the
# Hugging Face checkpoint it was converted from names `opus-2020-02-26.zip` as its original
# weights — **the same release `artifacts.py` pins for CTranslate2**, and the later of the
# two this pair publishes. So both engines run the same model here rather than two vintages
# of it, which is the thing that would otherwise silently explain any difference between
# them.
ONNX_OPUS_MT_RU_EN = OnnxTranslationModel(
    name="onnx-opus-mt-ru-en",
    pin=ModelPin(
        name="onnx-opus-mt-ru-en",
        repo_id="onnx-community/opus-mt-ru-en",
        revision="92ef0d550ca96ebd9cd5d13aab6ad41854d99a3d",
        licence="CC-BY-4.0",
        digests=_RU_EN_FILES,
    ),
    source_language="ru",
    target_language="en",
    licence="CC-BY-4.0",
    attribution=(
        "Russian-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. ONNX conversion by onnx-community. "
        "https://github.com/Helsinki-NLP/Opus-MT"
    ),
)

# Both French exports descend from the releases `artifacts.py` pins, and that was checked
# before either was fetched rather than assumed afterwards: each Hugging Face checkpoint the
# export was converted from names `opus-2020-02-26.zip` as its original weights, and
# onnx-community's own metadata names that checkpoint as `base_model`.
#
# It matters more here than for Russian. `fr-en` publishes two releases, and the other one is
# a **BPE** model that ADR 0032 refused for the CTranslate2 side. An ONNX export of that
# vintage would have run, produced plausible French, and quietly been a different model on
# one engine than on the other.
ONNX_OPUS_MT_EN_FR = OnnxTranslationModel(
    name="onnx-opus-mt-en-fr",
    pin=ModelPin(
        name="onnx-opus-mt-en-fr",
        repo_id="onnx-community/opus-mt-en-fr",
        revision="060ab253b6aae185f277c1e048fb0cf61a02c6c0",
        licence="CC-BY-4.0",
        digests=_EN_FR_FILES,
    ),
    source_language="en",
    target_language="fr",
    licence="CC-BY-4.0",
    attribution=(
        "English-French translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. ONNX conversion by onnx-community. "
        "https://github.com/Helsinki-NLP/Opus-MT"
    ),
)

ONNX_OPUS_MT_FR_EN = OnnxTranslationModel(
    name="onnx-opus-mt-fr-en",
    pin=ModelPin(
        name="onnx-opus-mt-fr-en",
        repo_id="onnx-community/opus-mt-fr-en",
        revision="f7e3c392bacfe300a66b0ddbd20de1d2dfc3b77e",
        licence="CC-BY-4.0",
        digests=_FR_EN_FILES,
    ),
    source_language="fr",
    target_language="en",
    licence="CC-BY-4.0",
    attribution=(
        "French-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26, "
        "licensed CC-BY-4.0. ONNX conversion by onnx-community. "
        "https://github.com/Helsinki-NLP/Opus-MT"
    ),
)

KNOWN_ONNX_MODELS: dict[str, OnnxTranslationModel] = {
    ONNX_OPUS_MT_EN_RU.name: ONNX_OPUS_MT_EN_RU,
    ONNX_OPUS_MT_RU_EN.name: ONNX_OPUS_MT_RU_EN,
    ONNX_OPUS_MT_EN_FR.name: ONNX_OPUS_MT_EN_FR,
    ONNX_OPUS_MT_FR_EN.name: ONNX_OPUS_MT_FR_EN,
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
