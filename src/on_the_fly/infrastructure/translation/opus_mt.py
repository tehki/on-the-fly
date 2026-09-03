"""Translation through CTranslate2, against an OPUS-MT model (ADR 0009).

The engine is CTranslate2, which this project already had transitively through
`faster-whisper`. The weights are Helsinki-NLP's OPUS-MT, converted from the publisher's
own Marian release — see `artifacts.py` for why the conversion is a build step rather than
something that gets pinned.

Two things here are not obvious and are the reason this module is more than a wrapper.

**Recogniser output has to be re-cased before it is translated.** `sherpa-onnx` emits
uppercase, unpunctuated text — ADR 0008 records `AFTER EARLY NIGHTFALL THE YELLOW LAMPS
WOULD LIGHT UP...` — and OPUS-MT was trained on ordinary cased prose. Measured on this
model, feeding it the uppercase form is not a cosmetic problem:

```text
THE MEETING IS AT THREE O'CLOCK ON TUESDAY
  as recognised     980 ms   ВТОРНИК ВСТРЕЧИ НА СОВЕЩАНИИ          <- not a translation
  sentence-cased    567 ms   Встреча состоится во вторник в три часа.
```

The long sample from ADR 0008 degrades further: yellow lamps become white ones and
"squalid" becomes "pickled", at 5596 ms against 1524 ms. Uppercase is out of distribution
for the sentencepiece vocabulary, so it fragments into far more pieces — which is why the
wrong answer is also the slow one. Re-casing fixes both, and its output matched
ordinary-cased reference text exactly on every sentence tried.

**Nothing is cached.** A translation cache would retain project content past its window,
which `docs/RETENTION_POLICY.md` does not permit and handbook 64F.2 forbids outright. The
same sentence spoken twice is translated twice; that is the intended cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class TranslationError(RuntimeError):
    """Translation could not be performed. Never raised to mean "the output was poor"."""


class UnsupportedPairError(TranslationError):
    """A language pair this translator does not serve was requested.

    Separate from `TranslationError` because refusing is a normal outcome with a correct
    caller response, while a translation failure is not (handbook 16).
    """


def sentence_case(text: str) -> str:
    """Re-case recogniser output into the shape the model was trained on.

    Deliberately simple: lowercase, then capitalise the first letter. It is not truecasing
    — it will not restore capitals on names — and the model tolerates that far better than
    it tolerates a fully uppercase sentence. The publisher ships a truecasing model slot
    (`source.tcmodel`) but it is **empty** for this pair, so there is no better option on
    offer from upstream.

    Text that is not fully uppercase is returned unchanged. A user typing ordinary prose,
    or a future recogniser that emits real casing, must not have its capitals destroyed.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    letters = [character for character in stripped if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        lowered = stripped.lower()
        return lowered[:1].upper() + lowered[1:]
    return stripped


# The three protocols below are the slices of sentencepiece and ctranslate2 this module
# actually uses, named so the tests need neither library nor an 80 MB model. Their first
# parameters are positional-only because that is how the real implementations declare them
# — matching on parameter *name* would make a fake the only thing that ever satisfied them.


class _Tokeniser(Protocol):
    def encode(self, text: str, /, *, out_type: Any = ...) -> Any: ...


class _Decoder(Protocol):
    def decode(self, pieces: Any, /) -> Any: ...


class _Engine(Protocol):
    def translate_batch(self, source: Any, /, **options: Any) -> Any: ...


class OpusMtTranslator:
    """Implements the `Translator` port for exactly one language pair.

    One pair per instance on purpose. An OPUS-MT model is directional — the en→ru model
    does not translate ru→en — so an instance that accepted any pair would be claiming a
    capability the weights do not have. A pair it does not serve is refused rather than
    attempted (handbook 46, and the same fail-closed choice `stream --language` makes).
    """

    def __init__(
        self,
        engine: _Engine,
        source_tokeniser: _Tokeniser,
        target_decoder: _Decoder,
        *,
        source_language: str,
        target_language: str,
        beam_size: int = 6,
    ) -> None:
        self._engine = engine
        self._source = source_tokeniser
        self._target = target_decoder
        self._source_language = source_language.lower()
        self._target_language = target_language.lower()
        # 6 is the publisher's own `decoder.yml` setting, not a guess. Lowering it is a
        # latency/quality trade that belongs in a measurement, not in a default.
        self._beam_size = beam_size

    @property
    def pair(self) -> tuple[str, str]:
        return (self._source_language, self._target_language)

    def __repr__(self) -> str:
        # No text, ever. This object handles EPHEMERAL project content and its repr goes
        # into tracebacks and logs (Article 14).
        return (
            f"OpusMtTranslator({self._source_language}->{self._target_language}, "
            f"beam_size={self._beam_size})"
        )

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        requested = (source_language.lower(), target_language.lower())
        if requested != self.pair:
            raise UnsupportedPairError(
                f"this translator serves {self._source_language}->{self._target_language}; "
                f"{requested[0]}->{requested[1]} was requested. Load the model for that "
                "pair rather than expecting this one to cover it."
            )

        prepared = sentence_case(text)
        if not prepared:
            # An empty final produces no translation rather than an empty one. A caption
            # renderer clearing the screen for nothing is the failure this avoids, and it
            # is the same choice BatchStreamingRecognizer makes for empty transcripts.
            return ""

        pieces = self._source.encode(prepared, out_type=str)
        try:
            results = self._engine.translate_batch([pieces], beam_size=self._beam_size)
        except Exception as exc:
            # The engine's own exceptions are not this module's contract, and they can
            # carry the source text. Translate at the boundary and do not echo the input.
            raise TranslationError(f"translation failed: {type(exc).__name__}") from exc

        hypotheses = getattr(results[0], "hypotheses", None)
        if not hypotheses:
            raise TranslationError("translation produced no hypothesis")
        # str() rather than a cast: the decoder is an untyped library boundary, and this is
        # the point where its output becomes this function's declared return type.
        return str(self._target.decode(hypotheses[0]))


def load(
    model_dir: Path | str,
    spm_dir: Path | str,
    *,
    source_language: str,
    target_language: str,
    beam_size: int = 6,
) -> OpusMtTranslator:
    """Build a translator from a converted model directory and its sentencepiece models.

    `ctranslate2` and `sentencepiece` are imported here rather than at module scope so the
    domain, the tests and a machine with no model can import this module freely — the same
    lazy-import shape `ModelStore._download` uses.
    """
    try:
        import ctranslate2
        import sentencepiece
    except ImportError as exc:  # pragma: no cover - exercised by the requirements install
        raise TranslationError(
            "ctranslate2 and sentencepiece are required to translate; install the runtime "
            f"requirements. Underlying error: {exc}"
        ) from exc

    model_path = Path(model_dir)
    spm_path = Path(spm_dir)
    for required in (spm_path / "source.spm", spm_path / "target.spm"):
        if not required.is_file():
            raise TranslationError(f"missing sentencepiece model: {required}")

    engine = ctranslate2.Translator(str(model_path), device="cpu", compute_type="int8")
    return OpusMtTranslator(
        engine,
        sentencepiece.SentencePieceProcessor(str(spm_path / "source.spm")),
        sentencepiece.SentencePieceProcessor(str(spm_path / "target.spm")),
        source_language=source_language,
        target_language=target_language,
        beam_size=beam_size,
    )
