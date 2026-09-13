"""Two people, two languages, one microphone (ADR 0047).

Everything this project does assumes one person is speaking one known language. That is half
a conversation: the reply comes back in the other language, and nothing recognises it. Picking
the source language before anyone speaks is the product's oldest limitation and the one its
first line — *speak without bounds with anyone worldwide* — most obviously promises away.

**The identification comes free with something already measured.** ADR 0043 looked for a
confidence threshold that separates "the right model for this audio" from the wrong one and
could not find one: the populations touch, because a hard clip recognised correctly scores
like a mismatched model. What it did not try is the comparison this makes — not *is this
score good enough*, but *which of these models scored better on the same audio*. That needs no
threshold at all, only an ordering.

Measured over every clip this project holds a published reference for, two recognisers on each
(ADR 0047): **8 of 10 utterances identified correctly.** Read the shape of that before the
score — only **two** of the ten decisions were comparisons at all, because the two recognisers
endpoint independently and most of the time one of them finalises alone. What carries this is
therefore mostly the exclusion rule; the comparison settles the minority of moments where both
models spoke, and on those two it was right both times, by 0.52 and 0.68. Both failures are one
model finalising alone over audio in the *other* language, which is precisely what ADR 0043
established no absolute score can catch.

**What it costs is the reason this is two languages and not three.** All three configurations
timed in one process so the load is the same load, 40.56 s of audio, best of two passes:
one recogniser is 0.45x real time, two are 0.96x, three are 1.11x — past the point of keeping
up with a live microphone, where being slower than the speech means losing it. Resident memory
is 157 MB, 306 MB and 432 MB against a 25 MB baseline, before any translation model.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from on_the_fly.domain.audio import AudioFormat, TranscriptEvent

# Below this the winner is not meaningfully ahead of the runner-up and the previous utterance's
# language is kept instead. A quarter of the narrowest correct margin measured, which was 0.52
# — cautious without being idle. It rests on two decisions, which is what the measurement had:
# every other one had a single model speaking, where there is no margin to be under.
CONFIDENT_MARGIN = 0.13


class ConversationRecognizer:
    """Several streaming recognisers over one microphone; the one that fits speaks.

    Implements `StreamingRecognizer`, so everything above it — the run, the store, the
    command line, the window — is unchanged. Each event it emits carries `language`, which
    is what lets a caller translate an utterance into whichever language the *other* person
    is speaking.

    Partials come from whichever language spoke last, because a decision cannot be made until
    an utterance has finished and showing three competing captions would be worse than showing
    one that is occasionally a sentence behind. The first utterance is judged the same way as
    every later one; only its partials are a guess.
    """

    __slots__ = ("_current", "_order", "_recognisers")

    # `Any` rather than the `StreamingRecognizer` port because this also calls `warm_up` and
    # `validate_format`, which the port does not carry: they belong to the concrete streaming
    # recognisers, and narrowing the port to suit one caller would make every implementation
    # answer for them.

    def __init__(self, recognisers: Mapping[str, Any]) -> None:
        if len(recognisers) < 2:
            raise ValueError(
                "a conversation needs at least two languages; one is what every other "
                "command already does"
            )
        self._recognisers = dict(recognisers)
        self._order = list(recognisers)
        self._current = self._order[0]

    @property
    def language(self) -> str:
        """Whichever language last won an utterance, or the first one configured."""
        return self._current

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def emits_partials(self) -> bool:
        """True. Callers must expect text to be replaced, not appended.

        A property, like every other recogniser's, and this was a *method* until a mutation
        sweep pointed at it: nothing reads it, so nothing noticed that
        `recognizer.emits_partials` was handing callers a bound method — truthy whatever it
        would have returned, which is the right answer here by accident and the wrong one
        for any caller asking `is False`.
        """
        return True

    def validate_format(self, audio_format: AudioFormat) -> None:
        for recogniser in self._recognisers.values():
            recogniser.validate_format(audio_format)

    def warm_up(self) -> None:
        for recogniser in self._recognisers.values():
            recogniser.warm_up()

    def reset(self) -> None:
        for recogniser in self._recognisers.values():
            recogniser.reset()
        self._current = self._order[0]

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        harvested = {
            code: recogniser.accept(frame) for code, recogniser in self._recognisers.items()
        }
        return self._decide(harvested)

    def finish(self) -> Sequence[TranscriptEvent]:
        harvested = {code: recogniser.finish() for code, recogniser in self._recognisers.items()}
        return self._decide(harvested)

    def _decide(self, harvested: dict[str, Sequence[TranscriptEvent]]) -> Sequence[TranscriptEvent]:
        """Emit one language's events: the winner's when an utterance ended, else the last.

        A frame that produced a final somewhere is a decision point. A frame that produced
        only partials is not — nothing has finished, so there is nothing to judge.
        """
        finals = {
            code: [event for event in events if event.is_final]
            for code, events in harvested.items()
        }
        if any(finals.values()):
            self._current = self._winner(finals)

        return tuple(_tagged(event, self._current) for event in harvested.get(self._current, ()))

    def _winner(self, finals: dict[str, list[TranscriptEvent]]) -> str:
        """Which language this utterance was in.

        A recogniser that produced nothing is out before any comparison: silence is not a
        low score, it is a model saying it has nothing to offer. Among the rest the best
        median confidence wins — and if the best is not clearly ahead, the language that was
        already being spoken keeps it, because a conversation does not usually change
        language mid-sentence and a coin toss between two near-equal scores would.
        """
        scored = {
            code: statistics.median(
                [event.confidence for event in events if event.confidence is not None]
            )
            for code, events in finals.items()
            if events and any(event.confidence is not None for event in events)
        }
        if not scored:
            # Nobody reported a confidence. Whoever produced a final at all is the only
            # evidence available, and the current language breaks the tie.
            # Never empty: `_decide` only asks this when some recogniser produced a final.
            speaking = [code for code, events in finals.items() if events]
            return self._current if self._current in speaking else speaking[0]

        ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)
        best, best_score = ranked[0]
        if len(ranked) > 1 and best_score - ranked[1][1] < CONFIDENT_MARGIN:
            return self._current if self._current in scored else best
        return best


def _tagged(event: TranscriptEvent, language: str) -> TranscriptEvent:
    """The same event, saying which language recognised it."""
    return replace(event, language=language)
