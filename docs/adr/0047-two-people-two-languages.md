# ADR 0047 — Two people, two languages, one microphone

**Status:** Accepted
**Date:** 2026-09-13
**Deciders:** @tehki
**Risk:** MEDIUM — a new mode that runs two recognisers and two translation models at once; the
one-language path is untouched

## Context

Every command in this project asks who is speaking before anyone speaks:

```bash
python -m on_the_fly listen --language en --translate-to ru
```

That serves one half of a conversation. English goes in and Russian comes out; the reply comes
back in Russian and nothing recognises it, because the pipeline was told there is one language
and it is English. The person holding the microphone has to stop, change a flag, and restart
between turns — and this project's first line is *speak without bounds with anyone worldwide*.

The obvious way to fix it is language identification, and
[ADR 0043](0043-what-the-streaming-recogniser-thought.md) already tried the obvious form of
that and **failed**. It went looking for a confidence threshold that separates "this is the
right model for this audio" from "this is the wrong one", and found the two populations
overlap:

> A hard clip recognised correctly scores like a mismatched model does. There is no line to
> draw, so no line is drawn.

That finding is about an *absolute* judgement — is this score good enough — and it stands. What
it never tried is the question a conversation actually asks: **not whether a score is good, but
which of two models scored better on the same audio.** A comparison needs no threshold at all,
only an ordering, and the audio being identical on both sides removes every variable ADR 0043
was defeated by. Clipping that drags one model's confidence down drags the other's down too.

## Decision

**`--conversation en:fr`, on `listen` and on `stream`.** Two streaming recognisers hear every
frame; whichever one is more confident about a finished utterance is the one whose text is
shown, and that utterance is translated into *the other* language. It takes the place of
`--language` and of `--translate-to`, and argparse refuses it beside either.

This is a real run of the shipped command, on 2026-09-13 — the published English clip and a
published French one in one file, a second of silence between them, because opening the
reference machine's microphone to make a demonstration is not something an agent should do
unasked. `--conversation` is on `stream` as well as `listen` for exactly that reason: a record
nobody can reproduce is a claim, not a record.

```bash
python -m on_the_fly stream conversation.wav --conversation en:fr --finals-only
```

```text
file          conversation.wav
language      English (en, streaming), French (fr, streaming)
model         streaming-en (local, verified, Apache-2.0)
model         streaming-fr (local, verified, Apache-2.0)
model load    10.17s
translation   opus-mt-en-fr on ctranslate2 (local, verified, CC-BY-4.0)
attribution   English-French translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26,
              licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT
translation   opus-mt-fr-en on ctranslate2 (local, verified, CC-BY-4.0)
attribution   French-English translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-26,
              licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT

  [en] [   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND
                          THERE THE SQUALID QUARTER OF THE BROTHELS
           → [fr] Après la tombée de la nuit, les lampes jaunes allumaient ici et là le
                  quartier sordide des maisons closes
  [fr] [   7.78s final  ] CE SITE CONTIENT QUATRE TOMBEAUX DE LA DYNASTIE HACHÉMÉNIDE ET
                          SEPT DES SASSANDIDES
           → [en] This site contains four tombs of the Hashemenid dynasty and seven of the
                  Sassandids
  [en] [   7.52s final  ] SUSID CONTENCA TONUD REGINIZZI AS SHE MAY NEED A SECT DE SASIN
                          NEED
           → [fr] Susid contenca tonud reginizzi car elle peut avoir besoin d'une secte de
                  sasin

audio         15.74s in 787 frames
wall time     15.46s
real-time     0.982x  (keeps up)  excludes model load
first text    1.12s into the audio
events        33 partial, 3 final
confidence    -0.43 median over 3 final(s)
speakers      en 2, fr 1 final(s)
translation   3 of 3 final(s), median 418ms, max 470ms
retention     clean - nothing retained, no deletion failed
```

**Two turns, both identified, both translated — and a third line that is the whole limitation
in one place.** The first two are the feature working: English recognised as English and
translated into French, then French recognised as French and translated back, with nothing
configured between them. The third is the English model's own reading of the French audio,
finalised at its own endpoint a moment after the French model had already spoken for that
region, and there is nothing in a confidence score that says so (ADR 0043). It is left in this
record rather than cropped out of it, because a reader deciding whether to use this mode needs
to see it.

Four decisions inside that:

- **The comparison is per utterance, not per run.** A conversation changes language every few
  seconds, so the decision point is a finished utterance — the first frame at which any
  recogniser produces a final.
- **A model with nothing to say is excluded before any comparison.** ADR 0044 measured that
  the French recogniser produces *no output whatever* on English speech: no partials, no
  finals, six endpoints with nothing in them. Silence is not a low score; it is a model
  declining to answer, and scoring it as though it had answered badly would be reading it
  wrongly.
- **A close call keeps the language already being spoken.** Below a margin of **0.13** the
  winner is not meaningfully ahead, and a coin toss between two near-equal scores would switch
  language inside a sentence. The number is a quarter of the narrowest correct margin measured
  — and the measurement below is honest that it applies to a minority of decisions.
- **Partials come from whoever spoke last.** Nothing can be judged until an utterance has
  finished, and three competing captions on screen would be worse than one that is occasionally
  a sentence behind.

The rest is unchanged on purpose. `ConversationRecognizer` implements the same
`StreamingRecognizer` port as a single model, so the run, the retention store, the level
monitor, the settling decorator and the summary are the ones that were already measured.
`TranscriptEvent` gained one field — `language` — and the CLI translates on the strength of it.

## What it identifies, measured

Every clip this project holds a published reference for — two English, three French — run
through an `en:fr` conversation on 2026-09-13. Each line is one *decision*: a frame at which
some recogniser finalised an utterance, and the scores it was decided on.

```text
clip                           utterance scores                    chose  spoken
en/0                           en  -0.25                              en   en
en/1                           en  -0.26                              en   en
                               en  -0.31                              en   en
                               en  -0.36                              en   en
fr/common_voice_fr_19364697    en  -0.87 fr  -0.35                    fr   fr
fr/common_voice_fr_19738183    en  -1.11 fr  -0.44                    fr   fr
fr/common_voice_fr_27024649    fr  -1.10                              fr   fr
                               en  -1.68                              en   fr   <- wrong
                               fr  -0.55                              fr   fr
                               en  -1.07                              en   fr   <- wrong

8 of 10 utterances identified correctly
the two decisions that were comparisons: margins 0.52 and 0.68, both correct
```

**Read the shape of that table before the score.** Only **two of the ten decisions were
comparisons at all.** The rest are one model finalising alone, because the two recognisers
endpoint independently: they do not agree on where an utterance ended, so "the same utterance"
is frequently not the same utterance for both of them. What carries this mode is therefore
mostly the exclusion rule — a model with nothing to say is out — and the comparison settles
the minority of moments where both models did speak. On those two it was right both times,
with margins of 0.52 and 0.68.

**Both errors are the same failure, and it is the one ADR 0043 named.** In the third French
clip the English model finalised twice at instants the French model did not, scoring −1.68 and
−1.07. With one model speaking there is nothing to compare against, and ADR 0043 established
that no absolute score distinguishes confident nonsense from a difficult clip recognised
correctly. So those two turns are attributed to English and translated the wrong way.

## What it costs, measured

All three configurations in one process on 2026-09-13, so the load they were measured under is
the same load. 40.56 s of audio over the same five clips, best of two passes, background load
2.3–3.9 on four cores:

| recognisers | resident | wall time | real time |
| --- | --- | --- | --- |
| baseline, none loaded | 25 MB | | |
| 1 (en) | 157 MB | 18.31s | **0.45x** |
| 2 (en, fr) | 306 MB | 38.84s | **0.96x** |
| 3 (en, fr, ru) | 432 MB | 44.86s | **1.11x** |

**Two fit inside real time and three do not.** That is the whole reason the flag takes a pair:
at 1.11x a third recogniser is slower than the speech it is listening to, and on a live
microphone that is not a slower answer but lost audio — `listen` would report overflows and
the words in them would be gone. Two at 0.96x is *inside* real time by 4%, which is thin
enough to state plainly rather than round away.

Memory is about 140 MB per recogniser and adds up honestly: 306 MB resident for a conversation
against 157 MB for one language, before either translation model is loaded.

Both halves of this are `scripts/measure_conversation.py`, so the next person can take them
again rather than trust them. A single-pass re-run on a quieter machine reproduces the
identification exactly and puts the cost at 0.54x and 0.98x.

## What this does not do

- **It does not identify a language this build has no recogniser for.** The comparison is
  between the models that are running. A third language spoken into an `en:ru` conversation is
  recognised as whichever of the two is less wrong, which is exactly the failure ADR 0043 said
  nothing here can detect: French audio through the English model comes back as confident
  nonsense.
- **It does not do three languages.** The flag takes a pair, because the cost above is what
  it is.
- **It does not run the recognisers in parallel.** They are fed in turn on one thread, which
  is where the 0.96x comes from. If a third language is ever wanted, threading this is the
  first place to look — `parallel.each` already exists for model loading and the same argument
  applies, since each recogniser releases the GIL while it decodes.
- **It does not appear in the window.** The desktop interface still picks one language from
  a list ([ADR 0016](0016-desktop-interface.md)). Wiring a mode with two captions
  into an interface built for one is a separate decision.
- ~~**It does not wait for a second opinion, and it does not de-duplicate a region of audio.**~~
  **Measured 2026-09-13, and both candidates are refused.** The decision is made at the first
  final, on whoever produced it, and a second model finalising over the same seconds later
  produces a second caption — the third line in the run above. The two answers to that were a
  **grace period** and **one utterance per region of audio**, and the thirty-fourth measurement
  took the number both of them rest on: where each model's endpointer actually fires.

  **The gaps are bimodal.** When the two endpointers agree they agree *exactly*, in the same
  20 ms frame, twice; when they disagree they disagree by **2.94 s**. One gap in four sits in
  between, at 0.06 s. So a grace period long enough to matter reaches one of the two wrong
  decisions and not the other, and charges every caption in every run for it — 100 ms against
  a p50 of 332 ms idle, 710 ms loaded.

  **De-duplication is closer and still refused.** Worked through the emitted spans it removes
  *both* spurious captions and loses nothing: 8 of 10 becomes 8 of 8. But the rule that does
  that — a final beginning inside one already emitted is a duplicate — is the same rule that
  **swallows an interruption**, showing nothing in its place. This repository has five
  recordings of one person reading and none of two people talking over each other, so that
  cost cannot be measured here. Trading a measured problem for an unmeasured one is not an
  improvement.
- **It is not tested on real two-speaker audio.** What is measured above is five
  single-speaker clips from published model releases, each run through both models. Nobody has
  yet held a conversation into this and counted the turns it got wrong.

## Review trigger

**A recording of two people, including one interruption.** That single artefact would settle
both questions this ADR left open: how many turns a real conversation gets wrong, and what
de-duplication would cost when somebody starts speaking before the other has stopped. Until it
exists, the spurious caption stays and is documented rather than papered over.

Also: if threading the recognisers makes a third language affordable.
