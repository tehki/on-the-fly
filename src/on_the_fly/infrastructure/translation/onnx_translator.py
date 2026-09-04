"""Translation on ONNX Runtime, for the platforms CTranslate2 cannot reach (ADR 0018).

CTranslate2 is faster and stays the desktop default. It has no mobile story — three open
unresolved Android issues and nothing for iOS (ADR 0017) — and ONNX Runtime does, shipping
official Android and iOS builds at the version this project already depends on. So the
second implementation of the `Translator` port exists to be the one that runs on a phone.

Both sit behind the same port. Nothing above `infrastructure/` knows which is loaded, which
is what ADR 0002 meant by porting the edges rather than the core.

**Generation is written out here rather than imported.** Sequence-to-sequence decoding is a
loop this module owns: encode once, then step the decoder one token at a time, carrying the
key/value cache. `transformers` would provide it and would bring torch, which is the
multi-gigabyte dependency ADR 0005 declined and the one thing that cannot go on a phone.
Roughly sixty lines against several hundred megabytes is a good trade, and the loop is
simple because the decoding strategy is greedy — measured as costing nothing against beam
search (ADR 0014).

**Two decoder graphs, not the merged one.** The export ships a merged decoder with a
`use_cache_branch` switch; its no-cache path fails on a zero-length encoder cache
(`Reshape` on `encoder_attn`, dimension zero). The separate `decoder_model` and
`decoder_with_past_model` pair has no such ambiguity: the first call produces the caches,
every later call consumes them.

**The encoder cache is computed once.** Cross-attention keys and values depend only on the
source sentence, so the with-past graph does not return them and this loop carries them
unchanged. Recomputing them per token would be the obvious mistake and would roughly double
the work.

**The publisher's `bad_words_ids` are honoured, and they are not optional.** OPUS-MT uses
one id for both padding and the decoder start token (62517), and `generation_config.json`
forbids generating it. A loop that ignores that runs into it: measured on the publisher's
own `ru-en` test set, one sentence in 300 produced `<pad>` repeated until the token budget
ran out — 9.7 seconds of work for output that was pure padding — and others carried a stray
`<pad>` mid-sentence. `transformers` applies this constraint as a matter of course, which is
exactly the sort of thing that goes missing when a generation loop is written out by hand.
It is applied here by masking those logits before the argmax.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from on_the_fly.infrastructure.translation.opus_mt import (
    DEFAULT_INTRA_THREADS,
    TranslationError,
    UnsupportedPairError,
    sentence_case,
)

# Long enough for any conversational turn, short enough that a degenerate model cannot spin.
# A translation that has not ended after this many tokens is a failure, not a long sentence.
# The publisher's own `max_length` is 512; this is tighter on purpose, because a live
# translator caption that arrives four seconds late has already failed the user.
MAX_NEW_TOKENS = 256

# Set on masked logits. `-inf` would propagate through any later arithmetic on the array;
# this is far below any real logit and stays finite.
_SUPPRESSED_LOGIT = -1.0e30


class OnnxTranslator:
    """Implements the `Translator` port using ONNX Runtime.

    One language pair per instance, refusing anything else — the same contract and the same
    reasoning as `OpusMtTranslator`: the weights are directional, and an instance accepting
    any pair would claim a capability they do not have.
    """

    def __init__(
        self,
        encoder: Any,
        decoder: Any,
        decoder_with_past: Any,
        source_pieces: Any,
        target_pieces: Any,
        vocabulary: dict[str, int],
        *,
        source_language: str,
        target_language: str,
        decoder_start_token_id: int,
        eos_token_id: int,
        suppressed_token_ids: tuple[int, ...] = (),
        max_new_tokens: int = MAX_NEW_TOKENS,
    ) -> None:
        self._encoder = encoder
        self._decoder = decoder
        self._decoder_with_past = decoder_with_past
        self._source_pieces = source_pieces
        self._target_pieces = target_pieces
        self._vocabulary = vocabulary
        self._inverse = {index: piece for piece, index in vocabulary.items()}
        self._unknown = vocabulary.get("<unk>", 1)
        self._source_language = source_language.lower()
        self._target_language = target_language.lower()
        self._start = decoder_start_token_id
        self._eos = eos_token_id
        # From the publisher's `bad_words_ids`. For OPUS-MT this is the pad token, which
        # shares an id with the decoder start token — see the module docstring for what
        # happens without it.
        self._suppressed = suppressed_token_ids
        self._max_new_tokens = max_new_tokens

        self._past_inputs = [
            i.name for i in decoder_with_past.get_inputs() if i.name.startswith("past_key_values")
        ]
        self._first_outputs = [o.name for o in decoder.get_outputs()]
        self._step_outputs = [o.name for o in decoder_with_past.get_outputs()]

    @property
    def pair(self) -> tuple[str, str]:
        return (self._source_language, self._target_language)

    def __repr__(self) -> str:
        # No text: this object handles EPHEMERAL project content and its repr reaches
        # tracebacks (Article 14).
        return f"OnnxTranslator({self._source_language}->{self._target_language})"

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        requested = (source_language.lower(), target_language.lower())
        if requested != self.pair:
            raise UnsupportedPairError(
                f"this translator serves {self._source_language}->{self._target_language}; "
                f"{requested[0]}->{requested[1]} was requested."
            )

        prepared = sentence_case(text)
        if not prepared:
            return ""

        try:
            return self._generate(prepared)
        except UnsupportedPairError:
            raise
        except Exception as exc:
            # Runtime errors can carry tensor contents and the source text. Translate at the
            # boundary without echoing the input.
            raise TranslationError(f"translation failed: {type(exc).__name__}") from exc

    def _generate(self, text: str) -> str:
        import numpy as np

        pieces = self._source_pieces.encode(text, out_type=str)
        ids = [self._vocabulary.get(piece, self._unknown) for piece in pieces] + [self._eos]
        source = np.array([ids], dtype=np.int64)
        mask = np.ones_like(source)

        hidden = self._encoder.run(None, {"input_ids": source, "attention_mask": mask})[0]

        first = dict(
            zip(
                self._first_outputs,
                self._decoder.run(
                    None,
                    {
                        "encoder_attention_mask": mask,
                        "input_ids": np.array([[self._start]], dtype=np.int64),
                        "encoder_hidden_states": hidden,
                    },
                ),
                strict=False,
            )
        )
        # Both caches start here. The decoder half grows each step; the encoder half depends
        # only on the source sentence and is carried unchanged.
        cache = {
            name: first["present" + name[len("past_key_values") :]] for name in self._past_inputs
        }
        logits = first["logits"]

        tokens: list[int] = []
        for _ in range(self._max_new_tokens):
            next_token = int(np.argmax(self._allowed(logits[0, -1])))
            if next_token == self._eos:
                break
            tokens.append(next_token)

            step = dict(
                zip(
                    self._step_outputs,
                    self._decoder_with_past.run(
                        None,
                        {
                            "encoder_attention_mask": mask,
                            "input_ids": np.array([[next_token]], dtype=np.int64),
                            **cache,
                        },
                    ),
                    strict=False,
                )
            )
            logits = step["logits"]
            for name in self._past_inputs:
                produced = "present" + name[len("past_key_values") :]
                if produced in step:
                    cache[name] = step[produced]

        return str(self._target_pieces.decode([self._inverse.get(t, "") for t in tokens]))

    def _allowed(self, row: Any) -> Any:
        """The final position's logits with forbidden tokens masked out.

        A copy, because the array belongs to the runtime's output and writing through it
        would be a side effect on someone else's buffer.
        """
        if not self._suppressed:
            return row
        import numpy as np

        masked = np.array(row, copy=True)
        masked[list(self._suppressed)] = _SUPPRESSED_LOGIT
        return masked


def load(
    model_dir: Path | str,
    *,
    source_language: str,
    target_language: str,
    intra_threads: int = DEFAULT_INTRA_THREADS,
    quantised: bool = True,
) -> OnnxTranslator:
    """Build a translator from a verified ONNX export directory.

    `intra_threads` defaults to 1 for the reason measured in ADR 0014: one translation taking
    every core is fine on an idle machine and catastrophic on a busy one, and a phone is
    never the idle machine.
    """
    try:
        import onnxruntime as ort
        import sentencepiece
    except ImportError as exc:  # pragma: no cover - exercised by the requirements install
        raise TranslationError(
            "onnxruntime and sentencepiece are required for ONNX translation; install the "
            f"runtime requirements. Underlying error: {exc}"
        ) from exc

    directory = Path(model_dir)
    suffix = "_int8" if quantised else ""
    options = ort.SessionOptions()
    options.intra_op_num_threads = intra_threads

    def session(name: str) -> Any:
        path = directory / "onnx" / f"{name}{suffix}.onnx"
        if not path.is_file():
            raise TranslationError(f"missing ONNX graph: {path}")
        return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])

    config_path = directory / "config.json"
    if not config_path.is_file():
        raise TranslationError(f"missing model config: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    generation_path = directory / "generation_config.json"
    if not generation_path.is_file():
        raise TranslationError(f"missing generation config: {generation_path}")
    generation = json.loads(generation_path.read_text(encoding="utf-8"))

    suppressed = _suppressed_tokens(generation)

    vocabulary_path = directory / "vocab.json"
    if not vocabulary_path.is_file():
        raise TranslationError(f"missing vocabulary: {vocabulary_path}")
    vocabulary = json.loads(vocabulary_path.read_text(encoding="utf-8"))

    return OnnxTranslator(
        session("encoder_model"),
        session("decoder_model"),
        session("decoder_with_past_model"),
        sentencepiece.SentencePieceProcessor(str(directory / "source.spm")),
        sentencepiece.SentencePieceProcessor(str(directory / "target.spm")),
        vocabulary,
        source_language=source_language,
        target_language=target_language,
        decoder_start_token_id=int(config["decoder_start_token_id"]),
        eos_token_id=int(config["eos_token_id"]),
        suppressed_token_ids=suppressed,
    )


def _suppressed_tokens(generation: dict[str, Any]) -> tuple[int, ...]:
    """The single-token entries of the publisher's `bad_words_ids`.

    Multi-token entries are refused rather than skipped. They forbid a *sequence*, which
    this greedy loop has no machinery to enforce, and quietly ignoring a constraint the
    publisher stated would be the same class of mistake as not reading it at all.
    """
    entries = generation.get("bad_words_ids") or []
    tokens: list[int] = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 1:
            raise TranslationError(
                "this model forbids a multi-token sequence, which the greedy loop cannot "
                f"enforce: {entry!r}"
            )
        tokens.append(int(entry[0]))
    return tuple(tokens)
