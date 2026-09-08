"""Translating a pair no single pinned model serves, by going through English.

French and Russian both stream, and until now they could not be translated into one
another: four of the six ordered pairs among the streaming languages worked. Helsinki-NLP
does publish direct `fr-ru` and `ru-fr` models, and they are not the route taken here. The
only ONNX exports of them come from publishers who declare no licence at all, and ADR 0018
already refused that publisher on exactly those grounds — so pinning the direct models
would serve the pair on CTranslate2 and never on ONNX, which is the engine that runs on a
phone. `test_every_pair_is_served_on_both_engines_or_neither` exists to stop that.

Chaining two pairs this project already pins costs no new artefact, no new publisher, and
no new licence question, and it works on both engines because both legs do.

**It is not a compromise in one direction and is in the other.** Measured over 1000
sentences of the publisher's own test sets, scored with this project's chrF2 against the
same references (ADR 0037):

```text
                 direct (publisher's own)   via English   difference
    fr -> ru              57.27               62.71         +5.44
    ru -> fr              65.99               61.05         -4.94
```

The two legs are high-resource pairs and land near 62 whichever way they are run; the
direct models vary far more. For `fr->ru` the chain beats the model it replaces. For
`ru->fr` it does not — and the comparison there is against a model this project cannot ship
on both engines, so the real alternative was not serving the pair at all.

**The intermediate never lands anywhere.** The English text between the two legs is
transient project content that exists as a local and goes out of scope when `translate`
returns; nothing stores it, so there is nothing to expire and nothing that could outlive
the ten seconds `docs/RETENTION_POLICY.md` allows. It is deliberately not put in the store:
adding a retention entry for a value that cannot outlive its own stack frame would make the
store's contents a worse description of what is being kept, not a better one.
"""

from __future__ import annotations

from on_the_fly.domain.audio.ports import Translator

# Every pair this project pins has English on one side, so it is the only language that can
# bridge any two of them. Asserted in the tests rather than assumed: a pinned pair with no
# English side would make this the wrong bridge and should say so.
PIVOT_LANGUAGE = "en"


class PivotTranslator:
    """Two translators in series, presented as one.

    Implements `Translator` so nothing above `infrastructure/` learns that this pair takes
    two hops — the same containment ADR 0018 relied on when a second engine arrived.
    """

    __slots__ = ("_first", "_second", "_source", "_target", "_via")

    def __init__(
        self,
        first: Translator,
        second: Translator,
        *,
        source_language: str,
        target_language: str,
        via: str = PIVOT_LANGUAGE,
    ) -> None:
        if source_language == target_language:
            raise ValueError(
                f"a pivot from {source_language} to itself translates nothing; "
                "resolve the pair directly or refuse it"
            )
        if via in (source_language, target_language):
            raise ValueError(
                f"cannot pivot {source_language}->{target_language} through {via}: one leg "
                "would be the identity, and the pair is either served directly or not at all"
            )
        self._first = first
        self._second = second
        self._source = source_language
        self._target = target_language
        self._via = via

    @property
    def via(self) -> str:
        """The bridging language, for a caller that wants to say so."""
        return self._via

    def __repr__(self) -> str:
        return f"PivotTranslator({self._source}->{self._via}->{self._target})"

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        """Translate through the bridge, or refuse a pair this route was not built for.

        An empty first leg short-circuits: the second model given an empty string would
        return whatever it hallucinates from nothing, and there is no sentence to carry.
        """
        if (source_language, target_language) != (self._source, self._target):
            raise ValueError(
                f"this route translates {self._source}->{self._target}, not "
                f"{source_language}->{target_language}"
            )

        bridged = self._first.translate(
            text, source_language=self._source, target_language=self._via
        )
        if not bridged.strip():
            return ""
        return self._second.translate(
            bridged, source_language=self._via, target_language=self._target
        )
