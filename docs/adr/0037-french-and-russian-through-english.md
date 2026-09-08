# ADR 0037 — French and Russian, through English

**Status:** Accepted
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — changes which pairs the product offers, and serves two of them with two
models instead of one

## Context

Three languages stream ([ADR 0031](0031-french-recognition.md)) and four pairs translate
([ADR 0032](0032-french-translation.md), [ADR 0033](0033-french-on-onnx.md)). Six ordered
pairs exist among three languages. The two missing ones are the same two: `fr->ru` and
`ru->fr`, refused with *no pinned translation model*.

That refusal is the one a user is most likely to hit by accident. English is the language
each of the other two is already paired with, so the pairs that work are the pairs where
somebody speaks or reads English. Two people who share no language at all are exactly who
this product is for, and they were the ones told no.

Nothing about the pair is unavailable. Helsinki-NLP publishes `fr-ru` and `ru-fr` as OPUS-MT
archives on the same object store the four pinned archives come from. The question is not
whether the models exist. It is what pinning them would cost, and whether they are better
than the route already paid for.

## The direct models serve one engine

[ADR 0033](0033-french-on-onnx.md) established the invariant this project now holds: every
pair it serves is served on **both** engines, and a test fails if a pair is ever pinned on one
and forgotten on the other. That exists because CTranslate2 has no mobile build
([ADR 0017](0017-any-hardware.md)), so a pair that works only there is a pair that does not
work where the product is going.

Searched 2026-09-09: `onnx-community`, the publisher [ADR 0018](0018-onnx-translation.md)
admitted under Article 12, **publishes no export of either direction**. The ONNX exports that
do exist are `Xenova/opus-mt-fr-ru`, `Xenova/opus-mt-ru-fr` and two `R4kSo1997` int8
repackagings, and **none of the four declares a licence**. ADR 0018 refused `Xenova` on
exactly this ground for Russian, and ADR 0007's rule is older than either: *no licence is not
permission.*

So pinning the direct models means either serving `fr<->ru` on the desktop engine only —
spending the invariant ADR 0033 just bought — or admitting an unlicensed artefact. Both are
worse than the pair staying refused.

## The route that is already paid for

Every pinned pair has English on one side. `fr->en` and `en->ru` are both already pinned, on
both engines, verified by digest, measured. Chaining them serves `fr->ru` with **no new
artefact, no new publisher, no new licence and no additional download** — and, because both
legs exist on both engines, without touching the ADR 0033 invariant.

The usual objection to pivoting is that errors compound: two models, two chances to be wrong,
and the second one cannot recover information the first one dropped. That is a real effect and
it is measurable, so it was measured rather than assumed.

## Measurement

The publisher's own test file for each direct pair carries three lines per record — source,
human reference, **and the direct model's own output**. So the baseline needed no download:
the model this project decided not to pin scored its own test set, and the pivot scored the
same 1000 sentences against the same references with the same metric. Both numbers below come
from one command per direction, the shipped one:

```bash
python scripts/measure_translation.py --pair fr ru \
  --test-file fr-ru.test.txt --limit 1000 --validate
```

| direction | direct model (publisher's own output) | this pivot, via English | difference |
| --- | --- | --- | --- |
| `fr->ru` | 57.27 | **62.71** | **+5.44** |
| `ru->fr` | **65.99** | 61.05 | −4.94 |

**The pivot is better in one direction and worse in the other**, by about the same margin
either way. It is not the consolation prize the compounding argument predicts. The likely
reason is visible in the four pairs already measured: `*->en` scores 71 and `en->*` scores 64
to 66, so a pivot's quality is dominated by its second leg, and `en->ru` at 64.51 is stronger
relative to the direct `fr->ru` than `en->fr` is relative to the direct `ru->fr`.

Latency is the cost that does not vary: two decodes, so twice the time, and measurement
found exactly that. p50 per sentence over the same 1000 was **564 ms** for `fr->ru` and
**562 ms** for `ru->fr`. Measured again against each pair's own first leg in one session on
the same 300 sentences, so the two numbers are comparable to each other rather than to a
differently loaded machine:

| | first leg alone | bridged | ratio |
| --- | --- | --- | --- |
| `fr->en` / `fr->ru` | 261 ms | 543 ms | 2.08x |
| `ru->en` / `ru->fr` | 272 ms | 547 ms | 2.01x |

Twice, and no worse than twice: nothing about chaining costs anything the second decode does
not. Translation is not on the path to first text ([ADR 0009](0009-translation.md)) — it runs
after recognition has finalised — so this spends a budget that is not the tight one.

## Decision

**Serve `fr->ru` and `ru->fr` by chaining two pinned models through English.** Six pairs, on
both engines, with no new artefacts.

- `resolve` tries a single artefact first and falls back to a two-leg route. A pinned pair is
  never bridged; two hops where one would do is slower and no better.
- **Both legs run on the engine that was asked for.** Falling back across engines is still
  refused ([ADR 0018](0018-onnx-translation.md)): a caller who chose ONNX because it is what
  their target hardware runs, and silently got CTranslate2, would be told the application
  works there when nobody has checked.
- **The bridge is visible, never silent.** The choice carries the language it goes through,
  `str()` prints `fr->en->ru` rather than `fr->ru`, and `name` and `attribution` name both
  models — CC-BY-4.0 asks to be told about the work it covers, and two works are used.
- **Both models load before the first sentence.** A pair that cannot be served should fail
  while the caller is still starting up, not midway through the first thing somebody says.
- A pair that cannot be bridged keeps the refusal it was already owed, which names the pair
  asked for and lists what is pinned. Only a pair with the *shape* of a bridge — neither side
  English — and a missing leg gets the extra sentence about the bridge, because only there is
  the bridge the part worth explaining.

## Retention

**The English between the two legs is not stored and gets no retention entry.** It is
transient project content that exists as a local inside `translate` and goes out of scope when
the call returns; nothing writes it, logs it or shows it. Article 6 asks what is *kept*, and a
declared entry for a value that cannot outlive its own stack frame would make the retention
store a worse description of what this product holds, not a better one — it would list
something no reaper can ever find.

## What this does not do

- **It does not make `fr<->ru` as good as a dedicated model would be in both directions.**
  `ru->fr` is 4.94 chrF2 behind the direct model, and that is the price of not admitting an
  unlicensed artefact. If `onnx-community` ever publishes the pair, the trade is worth
  re-opening — the numbers to beat are in the table above.
- **It does not pivot anything else.** English is the bridge because every pinned pair has
  English on one side, which a test asserts rather than assumes. A future pair that does not
  touch English would need this decision re-taken, not extended.
- **It does not change the latency budget.** Translation is not on the path to first text
  ([ADR 0009](0009-translation.md)); a bridged pair spends about twice as long on a stage that
  runs after recognition has already finalised.
- **It does not chain three models.** Two hops was measured. Three was not, and a route long
  enough to need a search is a different decision.

## Review trigger

When a licensed ONNX export of either direct pair appears, or when a fourth streaming language
is added — a fourth language turns six ordered pairs into twelve, and eight of them would be
bridged.
