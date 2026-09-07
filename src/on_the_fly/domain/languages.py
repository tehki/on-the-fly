"""The languages this project supports, and how well.

Support is not a boolean. A language this project can transcribe with sub-second latency and
a language it can transcribe eventually, badly, are both "supported" in a sense that would
mislead a user. So each language carries the tier it is actually served at, and the
application is expected to tell the truth about it.

```text
STREAMING → a model is pinned here; results appear while the speaker is talking
BATCH     → recognised an utterance at a time, several seconds behind, through Whisper
```

**The tier describes what this project serves, not what the world has published**
(ADR 0034). That distinction was lost for two days and is the reason this docstring is
explicit about it. ADR 0007 assigned STREAMING to all seven languages on the evidence that a
published streaming model existed for each, which is a fact about Hugging Face rather than
about this repository — and ADR 0031 then found that four of those models cannot be adopted:
the family covering five languages points at a licence file that is empty, and the one
Apache-2.0 Spanish model emits phonemes rather than words.

Meanwhile those four languages *are* served, an utterance at a time, by the Whisper model
`transcribe` already loads. Marking them STREAMING therefore managed to overstate and
understate them at once: it promised live captions that do not exist, and it made the command
line answer a request for German with advice about pinning a model rather than with the
working command. They are BATCH, and they carry a note saying why.

**`BATCH` is a claim about latency and not about quality**, which ADR 0035 had to say out
loud after measuring it: Whisper `tiny` scores 77% word error on clean read French against
14% for the pinned French streaming model, and returns a different sentence rather than a
flawed transcript. None of the four has been measured — no licence-clean test set with human
references exists for them — but French is a high-resource language on clean audio, which is
the easiest case, so the note those four carry says the batch engine is a fallback rather
than a substitute.

Tajik is why this module exists and is no longer in it. It had no streaming model, no
licence-clean batch model this project could load, and — after ADR 0009 — no licence-clean
translation model either, so ADR 0010 removed it rather than let three unverified stages
compound behind the word "supported".

Russian was BATCH for a day, on the finding that no licence-clean streaming model existed.
That finding was wrong — the model was in a third repository nobody had opened — and
ADR 0012 restored it to STREAMING with an Apache-2.0 pin. French joined it in ADR 0031. The
episode is why `BATCH` was kept defined while it had no members: the tier that stops a
language being described as better served than it is had been needed once and wrongly applied
once, and both were reasons to keep it. It has members again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecognitionTier(Enum):
    """How well a language is actually served."""

    STREAMING = "STREAMING"
    BATCH = "BATCH"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Language:
    """A supported language and an honest description of its support."""

    code: str
    name: str
    tier: RecognitionTier
    note: str = ""

    @property
    def is_streaming(self) -> bool:
        return self.tier is RecognitionTier.STREAMING

    @property
    def has_caveat(self) -> bool:
        """True when this language needs something said out loud before it is used."""
        return bool(self.note)

    def __str__(self) -> str:
        suffix = f" — {self.note}" if self.note else ""
        return f"{self.name} ({self.code}, {self.tier}){suffix}"


# Why the other four are not streaming, in a sentence a user can act on. It is read out by
# the command line when someone asks to stream one of them, so it has to complete "German is
# not a streaming language: ..." and be true.
_NOT_ADOPTED = "no licence-clean streaming model could be adopted for it (ADR 0031)"

# Tajik was the eighth and was removed by ADR 0010. Three languages stream: English and
# Russian from ADR 0008 and ADR 0012, French from ADR 0031. The other four are batch, and
# were wrongly marked streaming until ADR 0034.
SUPPORTED: dict[str, Language] = {
    "en": Language("en", "English", RecognitionTier.STREAMING),
    "ru": Language("ru", "Russian", RecognitionTier.STREAMING),
    "fr": Language("fr", "French", RecognitionTier.STREAMING),
    "es": Language("es", "Spanish", RecognitionTier.BATCH, _NOT_ADOPTED),
    "it": Language("it", "Italian", RecognitionTier.BATCH, _NOT_ADOPTED),
    "pt": Language("pt", "Portuguese", RecognitionTier.BATCH, _NOT_ADOPTED),
    "de": Language("de", "German", RecognitionTier.BATCH, _NOT_ADOPTED),
}


def resolve(code: str) -> Language:
    """Look up a language, or refuse.

    An unknown code is refused rather than passed to a model that would guess. A recogniser
    silently attempting a language nobody validated produces confident nonsense, which is
    worse for a translator than an error.
    """
    normalised = code.strip().lower()
    try:
        return SUPPORTED[normalised]
    except KeyError:
        known = ", ".join(sorted(SUPPORTED))
        raise KeyError(
            f"unsupported language {code!r}; this project supports: {known}. "
            "Adding one means finding a model, pinning it, and recording its tier."
        ) from None


def streaming_languages() -> tuple[Language, ...]:
    """The languages that can be recognised live."""
    return tuple(lang for lang in SUPPORTED.values() if lang.is_streaming)


def batch_languages() -> tuple[Language, ...]:
    """The languages that work, but not live.

    Empty from ADR 0010 until ADR 0034, which is the whole argument for having kept it: the
    guard the command line needs to refuse a streaming request existed before the languages
    that needed it did, so restoring them was a data change rather than a code change.
    """
    return tuple(lang for lang in SUPPORTED.values() if not lang.is_streaming)
