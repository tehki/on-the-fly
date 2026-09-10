# ADR 0044 — Speech went in and nothing came out

**Status:** Accepted. **Builds the thing [ADR 0043](0043-what-the-streaming-recogniser-thought.md)
named and did not build.**
**Date:** 2026-09-10
**Deciders:** @tehki
**Risk:** MODERATE — a message a user will act on, so a false one sends them to change a
setting that was right

## Context

ADR 0043 measured the streaming recogniser's confidence, found the populations for "right
model" and "wrong model" overlapping, and declined to draw a line through them. It ended by
pointing at something else in the same table:

> The strongest wrong-language signal in that table is not a score. **The French model emitted
> nothing whatever on English audio**, twice — a run that produces no finals from audible
> speech may be a better wrong-language detector than any threshold, and it is not built here.

This builds it, and the reason it is worth building is that nothing else in the product can
say it. Every check this project has is about the *audio*: too loud, too quiet, clipped, a
room amplified into speech-like energy. This one is about the **model**.

## The measurement

Five published clips through the shipped pipeline, each decoded by its own language's model
and by the other one:

| audio | model | partials | finals | endpoints with nothing in them |
| --- | --- | --- | --- | --- |
| `en/0` | English | 16 | **1** | 0 |
| `en/0` | French | **0** | **0** | **2** |
| `en/1` | English | 45 | **3** | 0 |
| `en/1` | French | **0** | **0** | **6** |
| `fr` ×3 | French | 9–18 | 1–2 | 0 |
| `fr` ×3 | English | 8–17 | 1–2 | 0 |

The French model on English speech does not produce a poor transcript. It produces **nothing
at all** — no partials, no finals — while its endpointer keeps firing on utterances it cannot
decode, and the level monitor reports `ok` throughout.

**Endpoints-with-nothing-in-them alone is not the signal**, which is the trap here. Ten
seconds of pure silence produces three of them, as does room noise, as does noise amplified to
peak 0.3. A run that fires on that would fire on every quiet room anyone ever points this at.

**What separates them is whether there was speech**, and this project already has the
component that answers that — the energy detector the batch path segments with. Measured on
the same audio:

| | speech detected |
| --- | --- |
| pure silence, 10 s | **0 of 500 frames** |
| room noise, 10 s | **0 of 500** |
| noise amplified to peak 0.3, 10 s | **0 of 500** |
| real speech, five clips | **151–542 frames**, 45–80% of each run |

## Decision

**When a voice activity detector heard at least half a second of speech and the recogniser
produced no finals at all, say so, and name the language that was asked for.**

```text
events        0 partial, 0 final
no text       10.8s of this audio is speech and none of it was recognised.
              If it is not French, --language is the thing to check.
```

- `StreamingRun` runs the same `EnergyVoiceActivityDetector` the batch path uses, purely to
  count. The transducer still does its own endpointing; nothing about segmentation changes.
- `StreamingStats` carries `speech_seconds`.
- Half a second, because below that the claim rests on a frame or two, and accusing a model on
  that basis is worse than saying nothing.

## What this does not do

- **It does not detect a wrong model that has something to say.** French audio through the
  English model comes back as confident nonsense with finals, and nothing here catches that —
  ADR 0043 measured why a confidence threshold cannot either.
- **It does not name the right language.** It says the audio is speech, that this model heard
  none of it, and which language was asked for. Identifying what was actually spoken is a
  different model this project does not pin.
- **It does not fire on the batch path.** `transcribe` has its own answer to the same question
  in ADR 0042, and its segmentation already uses this detector rather than counting with it.
- **It is not a diagnosis.** A model that produces nothing may be the wrong one, may be broken,
  or may be facing speech far enough from the microphone that a transducer trained on clean
  audio has nothing to offer. The message says what was observed and offers the one thing a
  user can change.

## Review trigger

If it ever fires on audio a human confirms was the right language and was recognisable — the
wording is advice, but a false one still sends somebody to change a setting that was correct.
