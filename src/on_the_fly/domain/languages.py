"""The languages this project supports, and how well.

Support is not a boolean. A language this project can transcribe with sub-second latency and
a language it can transcribe eventually, badly, are both "supported" in a sense that would
mislead a user. So each language carries the tier it is actually served at, and the
application is expected to tell the truth about it.

The tiers come from ADR 0007, which records the evidence: a search of 134 published
sherpa-onnx streaming model repositories, plus a licence check on every option found for the
one language that had none.

```text
STREAMING → a streaming model exists; results appear while the speaker is talking
BATCH     → recognised an utterance at a time, several seconds behind
```

Tajik is why this module exists and is no longer in it. It had no streaming model, no
licence-clean batch model this project could load, and — after ADR 0009 — no licence-clean
translation model either, so ADR 0010 removed it rather than let three unverified stages
compound behind the word "supported".

Russian was BATCH for a day, on the finding that no licence-clean streaming model existed.
That finding was wrong — the model was in a third repository nobody had opened — and
ADR 0012 restores it to STREAMING with an Apache-2.0 pin. The episode is why `BATCH` stays
defined with no members: the tier that stops a language being described as better served
than it is has now been needed once and wrongly applied once, and both are reasons to keep
it rather than reasons to delete it.
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


# Tajik was the eighth and was removed by ADR 0010. Every language here streams; Russian
# rejoined them in ADR 0012 once the model that had been missed was found and pinned.
SUPPORTED: dict[str, Language] = {
    "en": Language("en", "English", RecognitionTier.STREAMING),
    "ru": Language("ru", "Russian", RecognitionTier.STREAMING),
    "es": Language("es", "Spanish", RecognitionTier.STREAMING),
    "it": Language("it", "Italian", RecognitionTier.STREAMING),
    "fr": Language("fr", "French", RecognitionTier.STREAMING),
    "pt": Language("pt", "Portuguese", RecognitionTier.STREAMING),
    "de": Language("de", "German", RecognitionTier.STREAMING),
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
    """The languages that work, but not live. Empty since ADR 0010 removed Tajik.

    Kept rather than deleted: the CLI refuses to stream a non-streaming language, and that
    guard should exist before the language that needs it does, not after.
    """
    return tuple(lang for lang in SUPPORTED.values() if not lang.is_streaming)
