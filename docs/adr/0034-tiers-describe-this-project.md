# ADR 0034 — The tier describes what this project serves, not what the world has published

**Status:** Accepted. **Amends [ADR 0007](0007-supported-languages.md)**, whose language set
stands and whose tier assignment does not.
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes what the product claims about four languages

## Context

`domain/languages.py` opens by saying support is not a boolean, and that each language
"carries the tier it is actually served at". [ADR 0007](0007-supported-languages.md) then
assigned `STREAMING` to all seven languages on this evidence:

> Searched 134 published `sherpa-onnx` streaming model repositories … Spanish 4, French 3,
> Italian 2, German 2, Portuguese 1.

That is a fact about Hugging Face, not about this repository. The gap did not matter while
nobody could tell the difference. [ADR 0031](0031-french-recognition.md) made it visible by
trying to close it: of the five languages that had a published model and no pin, **four
cannot be adopted at all** — the `kroko` family covering all four points at a `LICENSE` file
that is zero bytes, and the one Apache-2.0 Spanish model emits IPA phonemes rather than
words.

[PR #60](https://github.com/tehki/on-the-fly/pull/60) had already stopped the interface
offering them, by deriving what it offers from the pin registries rather than from the tier.
So the code was honest and the registry it read was not.

## The tier was wrong in both directions at once

**It overstated them.** `STREAMING` is defined as *results appear while the speaker is
talking*. No model exists here that can do that for Spanish, Italian, Portuguese or German,
and after ADR 0031 none is coming soon.

**It also understated them.** Those four languages *are* served — an utterance at a time, by
the Whisper model `transcribe` already loads and pins. Verified rather than assumed:

```console
$ python -m on_the_fly transcribe recording.wav --language de --allow-download
model         tiny (local, verified)
  [   0.00s +6.52s] Nach der ersten Ereneinfall der Yellow Lamps wird light ab hier …
real-time     4.62x
```

(That is English audio forced to German, so the words are nonsense; what it establishes is
that the path runs, and that 4.62x real time is squarely what `BATCH` was defined to mean.)

The practical cost of the mislabelling was a bad error message. Asking to stream German got:

> German has no pinned streaming model yet (looked for `'streaming-de'`). Pin one with
> `scripts/pin_model.py` after checking its licence.

That is advice for someone working on this repository, handed to someone who wanted to
transcribe something. The guard for the correct message already existed and could not fire,
because it keys on the tier.

## Decision

**Move Spanish, Italian, Portuguese and German to `BATCH`, each carrying a note saying why.**
English, Russian and French stay `STREAMING`; they are the three with a pinned, measured
model. The language set is unchanged — this amends ADR 0007's tier assignment, not its
membership.

The same request now gets:

> German is not a streaming language: no licence-clean streaming model could be adopted for
> it (ADR 0031). Use `'transcribe'` instead, which runs it through the batch engine.

Three consequences fall out without code changes, because the machinery was already there:

- `has_caveat` has something to guard for the first time since ADR 0010 removed Tajik. It
  exists so a user interface cannot render the language set without deciding what to do
  about a caveat, and four languages now carry one.
- `batch_languages()` has members again. ADR 0012 argued for keeping the tier defined while
  it was empty; that argument is now paid off, because restoring it was a data change rather
  than a code change.
- The catalogue is unaffected. It already required a pin as well as a tier, so what the
  window and the command line offer does not move.

## What this does not claim

- **It does not claim these four languages work well.** Nobody here has measured Whisper
  `tiny` on any of them, and the README already says `tiny` is not accurate enough to ship a
  translator on. `BATCH` means *recognised an utterance at a time, several seconds behind*,
  which is what has been demonstrated — not a quality claim. ADR 0007's rule about Tajik
  applies unchanged: claiming a language is not performing in it.
- **It does not give them translation.** No OPUS-MT artefact is pinned for any of the four,
  in either direction, so they transcribe and stop.

  > **Two of them have it now** ([ADR 0038](0038-german-translation.md),
  > [ADR 0039](0039-italian-one-way.md)): German translates in both directions and Italian
  > into English, both still recognised an utterance at a time. Spanish and Portuguese are
  > blocked on the publisher's tokeniser rather than on effort.
- **It does not close the door.** One commit by `Banafo` adding a real licence file would
  make four languages evaluable again, and the tier is the first thing that would change.

  > **Re-verified 2026-09-10 and no longer true as written** — see the note in
  > [ADR 0007](0007-supported-languages.md). The licence file is still empty, and the
  > loadable artefacts have since moved behind a token, so it would now take a licence *and*
  > a public model in a form this project can load.

## Review trigger

When a streaming model is adopted for any of the four, which moves it back; or when anyone
measures Whisper's accuracy on them, which would let the note say something more useful than
why it is not streaming.
