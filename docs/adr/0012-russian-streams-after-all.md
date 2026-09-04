# ADR 0012 — Russian streams after all, and ADR 0011 was wrong

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — restores a capability the previous ADR removed
**Supersedes:** [ADR 0011](0011-russian-recognition.md), whose central finding does not hold

## What ADR 0011 concluded, and why it was wrong

ADR 0011 said no licence-clean streaming Russian model existed, and moved Russian to the
batch tier on that basis. It checked two repositories:

- `alphacep/vosk-model-ru` — Apache-2.0, and its own metadata says `non-streaming zipformer2`
- `alphacep/vosk-model-small-ru` — Apache-2.0, exported without streaming metadata

Both true. The conclusion drawn from them was not, because there is a **third** repository:

**`alphacep/vosk-model-small-streaming-ru`** — Apache-2.0, first-party, and it streams.

It says so in its name. Its `chunk64` encoder carries exactly the metadata the other two
lacked:

```text
comment          = streaming zipformer2
encoder_dims     = 192,256,256,256,256,256
decode_chunk_len = 128
left_context_len = 256,128,64,32,64,128
```

It loads as an `OnlineRecognizer` and transcribes the publisher's own sample at **0.103x
real time**, first text 1.44 s into 7.08 s of audio.

## How it was found

By asking upstream, and then by being told to read something that had been there all along.

The issue filed against sherpa-onnx asked whether the republication could declare its
licence. The maintainer's answer was two sentences: the model's `README.md` says where it
came from, please see the original repo. That README is one line long and names
`alphacep/vosk-model-small-streaming-ru`.

So the licence question had a published answer before it was asked, and the "no licence
declared" premise was answered by provenance rather than by metadata — the republication
inherits from an Apache-2.0 original that it names.

The same reply corrected a second thing. ADR 0011's evidence table presented
`OnlineRecognizer` failing on `alphacep/vosk-model-ru` as though it said something about the
model's suitability. The maintainer pointed out that loading a non-streaming model with a
streaming recogniser is a usage error, and that is right. The *conclusion* — that this model
is non-streaming — was correct and independently supported by its own metadata. The framing
implied the failure was evidence when it was a category error, and an outside reader was
entitled to read it that way.

## Decision

**Russian returns to the streaming tier**, with `alphacep/vosk-model-small-streaming-ru`
pinned by revision and per-file digest, Apache-2.0.

The `chunk64` variants are pinned rather than the `int8` ones: those are the exports carrying
streaming metadata. There is no int8 chunk64 build, so this pin is 95 MB against the English
model's 73 MB.

## What this changes structurally

**Model file layout became a per-pin property.** The recogniser previously hard-coded the
English model's filenames, which are named after a training epoch; the Russian files are
named after a chunk size and live in subdirectories. Neither is a convention. A
`StreamingLayout` now travels with each pin, and the recogniser still refuses to glob —
loading whatever ONNX file happens to be present remains the thing not to do.

**The English casing problem does not recur.** The English recogniser emits uppercase, which
mistranslates badly (ADR 0009). The Russian one emits lowercase, and that was tested rather
than assumed: on four conversational sentences, lowercase and properly-cased input produced
identical translations in three cases and a trivial difference in the fourth. All-caps is far
out of distribution for a sentencepiece vocabulary; lowercase is not. `sentence_case` already
leaves non-uppercase text alone, so no change was needed — but that is a measured result, not
a lucky one.

## Consequences

- **A conversation is live in both directions.** `stream --language ru --translate-to en`
  runs at 0.258x real time end to end, translation 871 ms, retention clean.
- The project claims seven streaming languages again. Two are now pinned and measured;
  five remain named on the strength of a published model existing.
- ADR 0011's `transcribe --translate-to` work is unaffected and still useful: it is the
  path for any language without a streaming pin.
- `RecognitionTier.BATCH` has no members again. It stays, and its record is now one correct
  use (Tajik) and one incorrect one (Russian for a day) — both arguments for keeping a tier
  that forces the question rather than for deleting it.

## What this cost, recorded deliberately

A day of the registry describing Russian as batch, an ADR asserting something false, and a
public issue asking a question whose answer was in a README the issue itself linked to.

The check that would have caught it takes ten seconds: read the provenance of the artefact
you already know works, before concluding that nothing works. ADR 0011 examined two
repositories, found them both wanting, and generalised to "none exists" — over a search
space it had not enumerated.

This is the same failure the rest of this project keeps finding in its own documents: a claim
that was reasonable when written, stated more strongly than its evidence supported, and never
re-checked. It is worth noticing that the policy stack's insistence on verifying before
claiming did not prevent it. What caught it was someone outside the project reading the claim
and knowing better.

## Review trigger

Before any language is described as unavailable. Enumerate the publisher's repositories
first, and read the README of anything that already works.
