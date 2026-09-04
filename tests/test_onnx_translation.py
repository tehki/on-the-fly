"""Tests for translation on ONNX Runtime (ADR 0018).

Offline and deterministic: the three graphs are fakes, so these run without the 400 MB
export. What they cover is the part that is genuinely this project's code rather than the
runtime's — the greedy decoding loop, which `transformers` would normally provide and which
is written out here to keep torch off a phone.

The loop has two failure modes that produce a plausible-looking translation rather than an
error, so both are asserted directly: **stopping at end-of-sequence** (a loop that ignores
it emits padding until the token budget runs out) and **carrying the encoder cache
unchanged** (the with-past graph does not return it, and a loop that expects it back either
crashes or silently re-attends to nothing).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from on_the_fly.infrastructure.translation import (
    ONNX_OPUS_MT_EN_RU,
    ONNX_OPUS_MT_RU_EN,
    OnnxTranslator,
    TranslationArtifactError,
    TranslationEngine,
    TranslationError,
    UnsupportedPairError,
    resolve_engine,
    resolve_onnx,
)
from on_the_fly.infrastructure.translation.onnx_translator import _suppressed_tokens, load

# Two layers, so a bug that happens to work for one layer's cache entries is visible.
LAYERS = 2
EOS = 0
START = 62517
VOCAB = {"<unk>": 1, "▁good": 10, "▁morning": 11, "▁the": 12, "▁meeting": 13}


class FakeNode:
    def __init__(self, name: str) -> None:
        self.name = name


def _past_names() -> list[str]:
    names = []
    for layer in range(LAYERS):
        for half in ("decoder", "encoder"):
            for part in ("key", "value"):
                names.append(f"past_key_values.{layer}.{half}.{part}")
    return names


def _cache_block(value: float, length: int = 1) -> Any:
    return np.full((1, 8, length, 64), value, dtype=np.float32)


PAD = START  # OPUS-MT reuses one id for padding and for the decoder start token.


def _logits(token: int, *, runner_up: int | None = None) -> Any:
    """Logits whose argmax over the last position is `token`, then `runner_up`."""
    array = np.zeros((1, 1, 64000), dtype=np.float32)
    array[0, -1, token] = 2.0
    if runner_up is not None:
        array[0, -1, runner_up] = 1.0
    return array


class FakeEncoder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_inputs(self) -> list[FakeNode]:
        return [FakeNode("input_ids"), FakeNode("attention_mask")]

    def get_outputs(self) -> list[FakeNode]:
        return [FakeNode("last_hidden_state")]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[Any]:
        self.calls.append(feed)
        return [np.zeros((1, feed["input_ids"].shape[1], 512), dtype=np.float32)]


class FakeDecoder:
    """The no-cache graph: produces the first token and both halves of the cache."""

    def __init__(self, first_token: int, runner_up: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._first = first_token
        self._runner_up = runner_up

    def get_inputs(self) -> list[FakeNode]:
        return [
            FakeNode("encoder_attention_mask"),
            FakeNode("input_ids"),
            FakeNode("encoder_hidden_states"),
        ]

    def get_outputs(self) -> list[FakeNode]:
        return [FakeNode("logits")] + [
            FakeNode("present" + name[len("past_key_values") :]) for name in _past_names()
        ]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[Any]:
        self.calls.append(feed)
        # Encoder-half entries carry a distinctive value, so a loop that recomputed or
        # dropped them would be visible in what the with-past graph is fed.
        return [_logits(self._first, runner_up=self._runner_up)] + [
            _cache_block(7.0 if ".encoder." in name else 1.0) for name in _past_names()
        ]


class FakeDecoderWithPast:
    """The with-past graph: consumes both halves, returns only the decoder half.

    That asymmetry is the real export's behaviour and the thing most likely to be got
    wrong, so the fake enforces it: a missing input raises, exactly as onnxruntime does.
    """

    def __init__(self, tokens: list[int], runner_up: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._tokens = list(tokens)
        self._runner_up = runner_up

    def get_inputs(self) -> list[FakeNode]:
        return [FakeNode("encoder_attention_mask"), FakeNode("input_ids")] + [
            FakeNode(name) for name in _past_names()
        ]

    def get_outputs(self) -> list[FakeNode]:
        return [FakeNode("logits")] + [
            FakeNode("present" + name[len("past_key_values") :])
            for name in _past_names()
            if ".decoder." in name
        ]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[Any]:
        for name in _past_names():
            if name not in feed:
                raise KeyError(f"missing required input: {name}")
        self.calls.append(feed)
        step = len(self.calls)
        token = self._tokens[step - 1] if step <= len(self._tokens) else EOS
        length = step + 1
        return [_logits(token, runner_up=self._runner_up)] + [
            _cache_block(1.0, length) for name in _past_names() if ".decoder." in name
        ]


class FakePieces:
    def __init__(self, pieces: list[str] | None = None) -> None:
        self.encoded: list[str] = []
        self.decoded: list[list[str]] = []
        self._pieces = pieces if pieces is not None else ["▁good", "▁morning"]

    def encode(self, text: str, /, *, out_type: Any = None) -> list[str]:
        self.encoded.append(text)
        return list(self._pieces)

    def decode(self, pieces: Any, /) -> str:
        self.decoded.append(list(pieces))
        return " ".join(str(piece) for piece in pieces).replace("▁", "")


def build(
    *,
    first_token: int = 10,
    then: list[int] | None = None,
    source: FakePieces | None = None,
    target: FakePieces | None = None,
    encoder: FakeEncoder | None = None,
    runner_up: int | None = None,
    suppressed: tuple[int, ...] = (),
    max_new_tokens: int = 256,
) -> tuple[OnnxTranslator, FakeEncoder, FakeDecoder, FakeDecoderWithPast]:
    encoder = encoder if encoder is not None else FakeEncoder()
    decoder = FakeDecoder(first_token, runner_up)
    with_past = FakeDecoderWithPast(then if then is not None else [11, EOS], runner_up)
    translator = OnnxTranslator(
        encoder,
        decoder,
        with_past,
        source if source is not None else FakePieces(),
        target if target is not None else FakePieces(),
        VOCAB,
        source_language="en",
        target_language="ru",
        decoder_start_token_id=START,
        eos_token_id=EOS,
        suppressed_token_ids=suppressed,
        max_new_tokens=max_new_tokens,
    )
    return translator, encoder, decoder, with_past


# --------------------------------------------------------------------------------------
# The decoding loop
# --------------------------------------------------------------------------------------


def test_it_decodes_until_end_of_sequence() -> None:
    translator, _, _, with_past = build(first_token=10, then=[11, EOS])

    result = translator.translate("good morning", source_language="en", target_language="ru")

    assert result == "good morning"
    # Two steps: one that produced token 11, one that produced EOS and ended the loop.
    assert len(with_past.calls) == 2


def test_end_of_sequence_is_not_emitted() -> None:
    """A loop that appends before checking puts the EOS token into the output."""
    target = FakePieces()
    translator, _, _, _ = build(first_token=10, then=[EOS], target=target)

    translator.translate("good morning", source_language="en", target_language="ru")

    assert target.decoded[-1] == ["▁good"]


def test_an_immediate_end_of_sequence_produces_no_text() -> None:
    translator, _, _, with_past = build(first_token=EOS)

    assert translator.translate("x", source_language="en", target_language="ru") == ""
    assert with_past.calls == []


def test_the_encoder_runs_once_however_long_the_output() -> None:
    """Re-encoding per token would roughly double the work for no change in the result."""
    translator, encoder, _, _ = build(first_token=10, then=[11, 12, 13, EOS])

    translator.translate("good morning", source_language="en", target_language="ru")

    assert len(encoder.calls) == 1


def test_the_encoder_cache_is_carried_unchanged() -> None:
    """The with-past graph never returns it; a loop that expects it back is broken."""
    translator, _, _, with_past = build(first_token=10, then=[11, 12, EOS])

    translator.translate("good morning", source_language="en", target_language="ru")

    for call in with_past.calls:
        for name in _past_names():
            if ".encoder." in name:
                assert call[name].shape == (1, 8, 1, 64)
                assert float(call[name].flat[0]) == 7.0, "the encoder half was recomputed"


def test_the_decoder_cache_grows_with_each_step() -> None:
    translator, _, _, with_past = build(first_token=10, then=[11, 12, EOS])

    translator.translate("good morning", source_language="en", target_language="ru")

    lengths = [call["past_key_values.0.decoder.key"].shape[2] for call in with_past.calls]
    assert lengths == [1, 2, 3]


def test_generation_is_bounded() -> None:
    """A model that never emits EOS must stop rather than spin."""
    translator, _, _, with_past = build(first_token=10, then=[11] * 50, max_new_tokens=4)

    translator.translate("good morning", source_language="en", target_language="ru")

    assert len(with_past.calls) == 4


def test_the_source_is_terminated_with_end_of_sequence() -> None:
    """Marian expects it; without it the encoder sees a sentence that never ended."""
    translator, encoder, _, _ = build()

    translator.translate("good morning", source_language="en", target_language="ru")

    ids = encoder.calls[0]["input_ids"][0].tolist()
    assert ids == [VOCAB["▁good"], VOCAB["▁morning"], EOS]


def test_unknown_pieces_become_the_unknown_token() -> None:
    translator, encoder, _, _ = build(source=FakePieces(["▁good", "▁qwertyuiop"]))

    translator.translate("good qwertyuiop", source_language="en", target_language="ru")

    assert encoder.calls[0]["input_ids"][0].tolist() == [VOCAB["▁good"], VOCAB["<unk>"], EOS]


def test_decoding_starts_from_the_configured_start_token() -> None:
    translator, _, decoder, _ = build()

    translator.translate("good morning", source_language="en", target_language="ru")

    assert decoder.calls[0]["input_ids"][0].tolist() == [START]


# --------------------------------------------------------------------------------------
# The publisher's bad_words_ids, which are not optional
# --------------------------------------------------------------------------------------


def test_a_forbidden_token_is_never_emitted() -> None:
    """OPUS-MT shares one id between padding and the decoder start token.

    Measured on the publisher's own ru-en test set before this was applied: one sentence in
    300 emitted `<pad>` until the token budget ran out — 9.7 s of work for output that was
    pure padding — and others carried a stray `<pad>` mid-sentence.
    """
    target = FakePieces()
    translator, _, _, _ = build(
        first_token=PAD, then=[EOS], runner_up=10, suppressed=(PAD,), target=target
    )

    result = translator.translate("good morning", source_language="en", target_language="ru")

    assert target.decoded[-1] == ["▁good"], "the forbidden token reached the output"
    assert result == "good"


def test_suppression_does_not_stop_generation() -> None:
    """The runner-up is taken and the loop continues; it does not end the sentence early."""
    translator, _, _, with_past = build(
        first_token=PAD, then=[PAD, EOS], runner_up=11, suppressed=(PAD,)
    )

    result = translator.translate("good morning", source_language="en", target_language="ru")

    assert result == "morning morning"
    assert len(with_past.calls) == 2


def test_without_suppression_the_forbidden_token_is_emitted() -> None:
    """The inverse of the test above, so it is testing the mask rather than the fake."""
    translator, _, _, _ = build(first_token=PAD, then=[EOS], runner_up=10)

    result = translator.translate("good morning", source_language="en", target_language="ru")

    assert result != "good"


def test_a_multi_token_constraint_is_refused_rather_than_skipped() -> None:
    """It forbids a sequence, which this loop cannot enforce. Ignoring it would be a lie."""
    with pytest.raises(TranslationError):
        _suppressed_tokens({"bad_words_ids": [[62517], [10, 11]]})


def test_no_constraint_is_a_valid_answer() -> None:
    assert _suppressed_tokens({}) == ()
    assert _suppressed_tokens({"bad_words_ids": [[62517]]}) == (62517,)


# --------------------------------------------------------------------------------------
# The port's contract, which is the same as the CTranslate2 implementation's
# --------------------------------------------------------------------------------------


def test_uppercase_input_is_recased_before_it_reaches_the_model() -> None:
    """The recogniser emits capitals; OPUS-MT mistranslates them (ADR 0009)."""
    source = FakePieces()
    translator, _, _, _ = build(source=source)

    translator.translate("GOOD MORNING", source_language="en", target_language="ru")

    assert source.encoded == ["Good morning"]


def test_a_pair_it_does_not_serve_is_refused() -> None:
    translator, _, _, _ = build()

    with pytest.raises(UnsupportedPairError):
        translator.translate("good morning", source_language="ru", target_language="en")


def test_empty_input_produces_no_translation() -> None:
    translator, encoder, _, _ = build()

    assert translator.translate("   ", source_language="en", target_language="ru") == ""
    assert encoder.calls == []


def test_a_runtime_failure_does_not_echo_the_source_text() -> None:
    """Runtime errors carry tensor contents; the boundary reports the type and nothing else."""

    class Exploding(FakeEncoder):
        def run(self, _outputs: Any, feed: dict[str, Any]) -> list[Any]:
            raise ValueError("graph failed on <the private thing that was said>")

    translator, _, _, _ = build(encoder=Exploding())

    with pytest.raises(TranslationError) as caught:
        translator.translate("something private", source_language="en", target_language="ru")

    assert "private" not in str(caught.value)


def test_the_repr_carries_no_text() -> None:
    translator, _, _, _ = build()

    assert repr(translator) == "OnnxTranslator(en->ru)"


def test_a_missing_file_is_named_rather_than_loaded_around(tmp_path: Any) -> None:
    """An export missing a file is a broken cache, and the path is what a user needs."""
    with pytest.raises(TranslationError) as caught:
        load(tmp_path, source_language="en", target_language="ru")

    assert "missing" in str(caught.value)
    assert str(tmp_path) in str(caught.value)


# --------------------------------------------------------------------------------------
# The pin
# --------------------------------------------------------------------------------------


def test_the_onnx_model_is_pinned_by_revision_and_digest() -> None:
    pin = ONNX_OPUS_MT_EN_RU.pin

    assert pin.is_pinned
    assert len(pin.revision) == 40, "a short revision is not a commit"
    assert pin.repo_id == "onnx-community/opus-mt-en-ru"


def test_every_file_the_loader_reads_is_covered_by_the_pin() -> None:
    """A file fetched but not digested is a file nothing verifies."""
    names = set(ONNX_OPUS_MT_EN_RU.pin.digests)

    expected = {
        "onnx/encoder_model_int8.onnx",
        "onnx/decoder_model_int8.onnx",
        "onnx/decoder_with_past_model_int8.onnx",
        "source.spm",
        "target.spm",
        "vocab.json",
        "config.json",
        # Carries bad_words_ids. An unverified constraint is not a constraint.
        "generation_config.json",
    }
    assert names == expected
    assert set(ONNX_OPUS_MT_RU_EN.pin.digests) == expected


def test_the_licence_matches_the_publisher_of_the_weights() -> None:
    """cc-by-4.0 is what travels inside Helsinki-NLP's archive; the mirror says otherwise."""
    assert ONNX_OPUS_MT_EN_RU.licence == "CC-BY-4.0"
    assert ONNX_OPUS_MT_EN_RU.pin.licence == "CC-BY-4.0"


def test_the_attribution_names_both_the_authors_and_the_converter() -> None:
    attribution = ONNX_OPUS_MT_EN_RU.attribution

    assert "Helsinki-NLP" in attribution
    assert "CC-BY-4.0" in attribution
    assert "onnx-community" in attribution


def test_both_pinned_pairs_are_served_on_both_engines() -> None:
    """A second engine that covers half the product is a second engine nobody can rely on."""
    assert resolve_onnx(("en", "ru")).name == "onnx-opus-mt-en-ru"
    assert resolve_onnx(("ru", "en")).name == "onnx-opus-mt-ru-en"


def test_the_two_exports_share_one_sentencepiece_pair() -> None:
    """OPUS-MT trains a pair on one joint vocabulary, so the digests cross over.

    A free cross-check that these are two directions of one model family rather than two
    unrelated repositories that happen to follow the same naming convention.
    """
    forward, backward = ONNX_OPUS_MT_EN_RU.pin.digests, ONNX_OPUS_MT_RU_EN.pin.digests

    assert forward["source.spm"] == backward["target.spm"]
    assert forward["target.spm"] == backward["source.spm"]
    assert forward["vocab.json"] == backward["vocab.json"]


def test_a_pair_with_no_onnx_export_is_refused() -> None:
    with pytest.raises(TranslationArtifactError):
        resolve_onnx(("en", "de"))


# --------------------------------------------------------------------------------------
# Choosing an engine
# --------------------------------------------------------------------------------------


def test_the_default_engine_is_ctranslate2() -> None:
    """It is the faster one, and the desktop has no reason to run the other (ADR 0018)."""
    assert resolve_engine(("en", "ru")).engine is TranslationEngine.CTRANSLATE2
    assert resolve_engine(("en", "ru")).name == "opus-mt-en-ru"


def test_asking_for_onnx_gets_the_onnx_artefact() -> None:
    choice = resolve_engine(("en", "ru"), TranslationEngine.ONNX)

    assert choice.name == "onnx-opus-mt-en-ru"
    assert choice.pair == ("en", "ru")


def test_a_pair_the_requested_engine_cannot_serve_does_not_fall_back() -> None:
    """Silently serving an unpinned pair on the other engine would claim mobile support."""
    assert resolve_engine(("ru", "en")).engine is TranslationEngine.CTRANSLATE2

    with pytest.raises(TranslationArtifactError):
        resolve_engine(("en", "de"), TranslationEngine.ONNX)
