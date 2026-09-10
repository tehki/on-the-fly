# on-the-fly

Live speech translation. Speak without bounds with anyone worldwide.

> **Status: it translates English, Russian and French, live.** Point it at a WAV file and it
> will find the utterances, transcribe them with a local, integrity-verified streaming model,
> and translate the finalised text with `--translate-to`. Measured end to end, both Russian
> directions keep up comfortably — 0.54x real time for English→Russian, 0.26x for
> Russian→English. French joined as captions only
> ([ADR 0031](docs/adr/0031-french-recognition.md)) and now translates in both directions
> against English ([ADR 0032](docs/adr/0032-french-translation.md)), on both engines
> ([ADR 0033](docs/adr/0033-french-on-onnx.md)); its translation stage is the fastest of the
> four, and no end-to-end figure has been taken for it. **All six pairs among the three
> streaming languages now work**: `fr↔ru`, which no single pinned model serves, goes through
> English with no new models on either engine
> ([ADR 0037](docs/adr/0037-french-and-russian-through-english.md)). **German joined as a
> translation target** ([ADR 0038](docs/adr/0038-german-translation.md)) — it does not stream,
> and it does not have to: twelve of the twelve ordered pairs among English, Russian, French
> and German now resolve, half pinned and half bridged. **Italian is the mirror**
> ([ADR 0039](docs/adr/0039-italian-one-way.md)) — a source that is not a target, four more
> pairs, and the only one of the remaining three languages that could be taken at all.
>
> **Three of the seven languages stream**, using sherpa-onnx with pinned Apache-2.0 models
> ([ADR 0008](docs/adr/0008-sherpa-onnx-streaming.md),
> [ADR 0012](docs/adr/0012-russian-streams-after-all.md),
> [ADR 0031](docs/adr/0031-french-recognition.md)) — English at 0.399x with first text 1.10 s
> into the audio. The other four run an utterance at a time through Whisper, which is a
> statement about latency rather than throughput: `tiny` decodes English at 0.279x, so it is
> not slow, it simply has nothing to say until the speaker stops — and
> [ADR 0035](docs/adr/0035-the-batch-tier-measured.md) measured what it says when it does.
> Measurements in [docs/PERFORMANCE_BUDGET.md](docs/PERFORMANCE_BUDGET.md).

## Languages

Seven, at two tiers ([ADR 0007](docs/adr/0007-supported-languages.md),
[ADR 0034](docs/adr/0034-tiers-describe-this-project.md)):

**Streaming — English, Russian, French.** A model is pinned, verified and measured here, and
text appears while you are still talking.

**Batch — Spanish, Italian, Portuguese, German.** `transcribe` runs them through Whisper, an
utterance at a time and several seconds behind. That is a statement about latency, and
[ADR 0035](docs/adr/0035-the-batch-tier-measured.md) is the one about quality: Whisper `tiny`
scores **77.1% word error on clean read French**, where the pinned French streaming model
scores 14.3% on the same clips. It does not return a flawed transcript — it returns a
different sentence.

**A larger Whisper was the obvious answer and it is not enough**
([ADR 0041](docs/adr/0041-a-larger-whisper.md)). `base` and `small` are now pinned and
measured on the same clips:

| | English, 66 words | French, 35 words | French real time |
| --- | --- | --- | --- |
| `tiny` | 6.1% | 68.6% | 2.79x |
| **`base`, the new default** | **4.5%** | **54.3%** | **1.79x** |
| `small` | 4.5% | **34.3%** | 5.07x |
| the pinned French streaming model | — | **14.3%** | 0.50x |

English is solved: at `base` every remaining error is orthography, and `PARENT`, which `tiny`
heard as `parrot`, is right. French halves and is still not usable — `small` returns a legible
transcript of the right sentence, at five times real time, which for a six-second utterance is
thirty seconds. `base` is the default because it beats `tiny` everywhere measured and, on
French, is also **faster**: a model that returns a different sentence spends longer returning
it.

**And when it does that, it now says so** ([ADR 0042](docs/adr/0042-the-model-saying-it-failed.md)).
ADR 0021 recorded that nothing here could detect a recogniser inventing words — the level
checks catch bad audio, and this failure happens on good audio. faster-whisper applies its own
quality checks to a greedy decode and, when they reject it, retries the segment by sampling.
The temperature it ended up using was being thrown away. It separates the measured clips five
times out of five, where the log probability everyone quotes separates them once in five:

```text
  [   0.00s +6.32s] sur la scénère majeur littéra le conseil de la taille de 100 pierres…
                    ! the model rejected its own first answer here, confidence -0.99;
                      treat this as unrecognised
```

The text is printed either way. A reader who knows the language judges a transcript better than
a threshold does, and a product that quietly dropped what it was unsure about would be hiding
its own failures. What changes is that nobody is left alone with a fluent sentence and no
reason to doubt it.

**The streaming side reports a number and refuses to judge it**
([ADR 0043](docs/adr/0043-what-the-streaming-recogniser-thought.md)). sherpa-onnx does report a
log probability per token, so `stream` and `listen` now print
`confidence -0.35 median over 3 final(s)`. What it will not do is warn on it: measured across
five clips decoded by the right model and the wrong one, the worst correct run scores -0.827
and the best wrong one -0.875, and the clip responsible is the one Whisper also fails on —
hard audio, recognised correctly, scoring like a mismatched model. An earlier draft warned at
-0.75 and told a user their French was not French while transcribing it correctly. The
strongest signal in that table is not a score at all: **the French model emitted nothing
whatever on English audio**, twice.

**So that is what the product says instead**
([ADR 0044](docs/adr/0044-speech-went-in-and-nothing-came-out.md)). Every other check here is
about the audio — too loud, too quiet, clipped, a room amplified into speech-like energy. This
one is about the model:

```text
events        0 partial, 0 final
no text       10.8s of this audio is speech and none of it was recognised.
              If it is not French, --language is the thing to check.
```

The trap is that ten seconds of silence also produces nothing, as does room noise, as does
noise amplified to peak 0.3 — each of them firing the endpointer three times with nothing in
between. What separates them is whether there was speech, and the energy detector the batch
path segments with answers that cleanly: **0 speech frames of 500 for silence and every kind
of noise tried, 151 to 542 for real speech**. So the finding needs both, and half a second of
speech at minimum, because accusing a model on the strength of one frame is worse than saying
nothing.

Russian, measured the same day, does the opposite: `tiny` differs from the pinned Russian
model by **2 words in 12**, and both are spellings rather than misrecognitions. Two
high-resource languages on clean read speech, a factor of four apart.

So none of those four has been measured, and **French does not predict them** — an inference
that it did was made in ADR 0035 and withdrawn there the same day. What is known is that the
batch engine's quality varies enormously by language and can be catastrophic, which is why
the command line calls it **a fallback rather than a substitute** when it refuses to stream
one of them.

Those four were marked streaming until ADR 0034, on ADR 0007's evidence that a published
streaming model existed for each — which is a fact about Hugging Face rather than about this
repository. It managed to overstate and understate them at once: it promised live captions
that do not exist, and it made the command line answer a request for German with advice about
pinning a model instead of naming the command that works.

**French is the third** ([ADR 0031](docs/adr/0031-french-recognition.md)), Apache-2.0 and
trained on Common Voice. The publisher reports **10.57% word error** on the full Common Voice
French test set for the exact checkpoint and decoding method pinned here, and it decodes at
essentially English's speed — 0.868x against 0.804x median over six paired runs on identical
audio, on a machine already carrying a load average of 6 to 8 on four cores.

**And it translates, both ways against English**
([ADR 0032](docs/adr/0032-french-translation.md)). Two more OPUS-MT archives, CC-BY-4.0 read
out of the archive rather than off a model page. Measured on 1000 sentences of each pair's
own publisher test set, alongside the two directions that already shipped:

| pair | chrF2 vs human references | translation p50 |
| --- | --- | --- |
| `en→ru` | 64.51 | 330 ms |
| `ru→en` | 71.46 | 319 ms |
| **`en→fr`** | **66.31** | **254 ms** |
| **`fr→en`** | **71.38** | **398 ms** |

French is the fastest of the four and sits between the Russian directions on quality.

**It runs on the portable engine too** ([ADR 0033](docs/adr/0033-french-on-onnx.md)), so
every pair this project serves is now served on both engines — a new test fails if one is
ever pinned on one engine and forgotten on the other. The two agree to within a rounding
error:

| pair | CTranslate2 | ONNX | difference |
| --- | --- | --- | --- |
| `en→fr` | 66.31 | 66.26 | −0.05 |
| `fr→en` | 71.38 | **71.45** | +0.07 |

That is closer than the Russian pair, where ADR 0018 measured a 0.29 chrF2 gap and traced it
to the export rather than the quantisation. It is also the only evidence available that these
third-party exports carry Helsinki-NLP's weights: no digest connects an ONNX graph to a
Marian archive, so the check is behavioural, and two independently converted artefacts
scoring within 0.07 of the archive-derived conversion is what "the same model" looks like
from outside.

**Which model, checked before either export was fetched.** `fr-en` publishes two releases and
[ADR 0032](docs/adr/0032-french-translation.md) refused one of them for being BPE; an ONNX
export of that vintage would have loaded, produced plausible French, and quietly been a
different model on one engine than the other. The chain is two declared links and both were
followed for all four pairs: `artifacts.py` pins an exact `.zip`, the Hugging Face checkpoint
names that archive as its original weights, and `onnx-community` names that checkpoint as its
base model.

**Choosing which release to pin took two rounds, and the second one is the interesting one.**
Both directions publish two releases; the publisher's own re-evaluation over twenty test
suites picks the 2020 release for `en→fr` and the *2019* one for `fr→en`. That second answer
is wrong, and no score table could say so: the 2019 `fr-en` release is a **BPE** model —
`source.bpe`, `target.bpe` — where every artefact here tokenises with sentencepiece. Loading
it means admitting a BPE implementation on every user's machine to buy 0.11 chrF2. Both
directions take the 2020 release.

**And French and Russian now translate into each other, through English**
([ADR 0037](docs/adr/0037-french-and-russian-through-english.md)). Three streaming languages
make six ordered pairs; four were served and the two missing ones were the two where neither
speaker knows English — which is the case this product exists for. They are served by chaining
two pinned models, with **no new artefact, publisher, licence or download**, on both engines,
because both legs already run on both.

The direct models exist and are not used. `onnx-community`, the publisher
[ADR 0018](docs/adr/0018-onnx-translation.md) admitted, publishes no export of either
direction, and the exports that do exist declare no licence at all — the same ground ADR 0018
refused `Xenova` on. Pinning the Marian archives alone would serve the pair on the desktop
engine only, spending the invariant ADR 0033 had just bought.

The compounding-error objection is real and was measured rather than assumed. The publisher's
test file carries the direct model's own output as its third line, so the model this project
declined to pin scored its own test set, and the bridge scored the same 1000 sentences against
the same references with the same metric:

| pair | direct model | via English | difference | translation p50 |
| --- | --- | --- | --- | --- |
| **`fr→ru`** | 57.27 | **62.71** | **+5.44** | 564 ms |
| **`ru→fr`** | **65.99** | 61.05 | −4.94 | 562 ms |

Better one way and worse the other, by about the same margin — not the consolation prize the
objection predicts. The reason is visible in the four pairs above: a bridge is dominated by its
second leg, and `en→ru` is stronger relative to the direct `fr→ru` than `en→fr` is relative to
the direct `ru→fr`. What does not vary is the cost: two decodes, so twice the time — measured
against each pair's own first leg in one session, 2.08x and 2.01x, which is what "twice" looks
like when nothing else is being paid for. Translation runs after recognition has finalised, so
it is not the tight budget.

The bridge is never silent. The route prints as `fr->en->ru`, the attribution names both
models, and both load before the first sentence rather than the second one failing midway
through what somebody just said — **at the same time as each other**, and as the recogniser
([ADR 0040](docs/adr/0040-loading-two-models-at-once.md)), because nothing in one needs the
other and in series that was the slowest thing this project does: 21.3 s from launch to first
text, now 16.9 s.

**Two models are also twice the memory, and that had never been measured.** The budget has
carried a 1200 MB resident-memory target since it was written and nothing had ever checked it;
`scripts/measure_memory.py` now does. A bridged pair on the portable engine — the worst
configuration this project ships — holds **1109 MB**, which is 92% of the target, against 671
MB for a direct pair on the same engine and 216 MB for captions with no translation at all.
The budget holds with 91 MB to spare. It should have been known before a second model was
added to a run rather than after.

**German translates in both directions, and does not stream**
([ADR 0038](docs/adr/0038-german-translation.md)). Four of the seven languages could be
transcribed and translated into *nothing at all*, which for a product whose first line is
"live speech translation" is four sevenths of the list doing the half that is not the point.
The two questions turn out to be independent: a translation target needs a translation model
and nothing else, so **somebody speaking English, French or Russian can now be read in German**
without any streaming model for German existing.

| pair | theirs (beam 6) | ours (greedy) | p50 | CTranslate2 vs ONNX |
| --- | --- | --- | --- | --- |
| **`de→en`** | 71.61 | **70.82** | 270 ms | 70.82 / 70.47 |
| **`en→de`** | 66.40 | **65.48** | 287 ms | 65.48 / 65.45 |

Six pinned pairs on two engines, and six more bridged: twelve of the twelve ordered pairs among
English, Russian, French and German resolve. `de↔ru` has no other route — Helsinki-NLP
publishes no German-Russian model at all — while `de↔fr` does, and there the bridge loses by
3.66 to 4.52 chrF2 to direct models this project still cannot pin, because `onnx-community`
publishes no `de-fr` export and its `fr-de` export declares no licence. A bridge lands near its
second leg whatever the pair: it beats a weak direct model and loses to a strong one, and both
results are written down.

**Spanish is the one that cannot be taken.** `es-en` and `en-es` publish one release each and
the publisher's own manifest calls it `normalization + tokenization + BPE`, where every
artefact here tokenises with sentencepiece. German publishes three releases per direction and
two of them are the same trap; the pin is an exact URL and a test reads the year out of it. The
largest of the four remaining languages is blocked on a release somebody else has not made.

**Italian goes the other way: a source that is not a target**
([ADR 0039](docs/adr/0039-italian-one-way.md)). Somebody speaking Italian can be understood in
English, French, German or Russian; nobody can be answered in Italian. `en-it` publishes a
sentencepiece archive that would convert here — what it has no usable ONNX export of is the
other engine, and a pair served on the desktop and never on a phone is the invariant ADR 0033
bought.

| | theirs (beam 6) | ours (greedy) | p50 | CTranslate2 vs ONNX |
| --- | --- | --- | --- | --- |
| **`it→en`** | 80.44 | **79.85** | **177 ms** | 79.85 / 79.66 |

That 79.85 is the highest score in this project and the one to be most careful with: these are
different sentences from every other row, and chrF2 across pairs is not comparable. What is
comparable is the 0.59 behind the publisher's own output **on identical text** — the smallest
greedy cost measured here — and the 177 ms, which makes it the fastest pair served.

**The refusal is the interesting half.** `onnx-community/opus-mt-en-it` exists, in the
organisation ADR 0018 admitted, and is a bare upload: no README, no card metadata, no licence,
no base model. That admission was never about a name — it rested on a repository declaring
`cc-by-4.0` and naming the checkpoint it converted, which is evidence about an artefact.
Treating it as a namespace would make the review a formality performed once.

**Portuguese cannot be taken at all.** Its only OPUS-MT releases are BPE in both directions.
There are sentencepiece Portuguese models in the Tatoeba-MT-models bucket, and no ONNX export
of them exists — `onnx-community` has exported two `opus-mt-tc-big-*` models and both are
Turkish. That road ends at a desktop-only pair.

**The other four do not stream, and that is not waiting on effort.** They are waiting on two
specific things, both somebody else's to fix — one commit adding a real licence file would make
four languages evaluable again:

| | |
| --- | --- |
| The `kroko` family — `es`, `it`, `de`, `pt` and also `fr`, from one publisher, and ADR 0007's strongest lead | Its republications say only *"See license at Banafo/Kroko-ASR"*. That repository declares `license: other`, `license_name: test`, `license_link: LICENSE` — and **the LICENSE file is empty**, zero bytes, unchanged since 2025-01-29. A README saying "our community models are CC-BY-SA" is prose, not a grant. ADR 0007's own rule, written about Tajik: *no licence is not permission.* |
| `bookbot/…-streaming-robust-es-v0` — genuinely Apache-2.0, and the smallest model found | It is a **phoneme recogniser**. Its vocabulary is 37 IPA symbols where the English pin has 502 word-pieces; it emits `["w", "ɑ", "ʃ", "i", "ɑ"]`, not words. Unreadable as a caption and untranslatable as input. |

**Tajik was the eighth and has been removed** ([ADR 0010](docs/adr/0010-drop-tajik.md)). It
had no streaming model anywhere, no licence-clean batch model this project could load
without a multi-gigabyte dependency, and no licence-clean translation model either. Three
unverified stages behind the word "supported" is not support, and nobody here can read Tajik
well enough to tell when it goes wrong. It comes back when a model does.
>
> The microphone adapter now runs against real hardware: it opens a device, yields
> correctly sized frames at the right cadence, releases on every exit path, and maps real
> ALSA failures to typed errors. What has **not** been verified is capture of *usable*
> audio — the machine it was tested on has an input device that produces saturated,
> DC-offset garbage, and raw `sounddevice` produces the same, so that is the hardware
> rather than the adapter. Recognition from a live microphone remains untested.
>
> Capture works on every input device on the reference machine. The adapter negotiates a
> rate the device accepts and resamples to 16 kHz
> ([ADR 0013](docs/adr/0013-capture-rate-negotiation.md)), and reads through PortAudio's
> callback rather than blocking reads, which was aborting the process on one device
> ([ADR 0015](docs/adr/0015-callback-capture.md)).
>
> **That resampler was broken until 2026-09-07 and it mattered.** It appended each output
> block's whole allocated buffer rather than the samples in it, so it emitted 1.19x the audio
> it was given — the surplus being stale samples from earlier blocks, not silence. Recognised
> through it, `THE SQUALID QUARTER OF THE BROTHELS` came back as `WHILE ITS WATER AT THE
> BOTTOM`: **38.9% word error on a clip that scores 0.0% through a file.** Invented words in a
> fluent sentence, which is the one failure a user cannot catch themselves.
>
> It survived because the whole test suite reads files, and a file at 16 kHz never touches
> the resampler. The bug was only ever on the microphone path.
>
> **Part of the "saturated hardware" turned out to be the capture path powering up**
> ([ADR 0020](docs/adr/0020-capture-settling.md)). A cold session begins pinned at the
> negative rail — DC −1.0, 100% of samples clipped, no signal at all — and takes about 1.8 s
> to centre; `arecord` shows the same, and a session opened moments later shows none of it.
> That transient is now measured and discarded before it reaches the recogniser, adaptively:
> **1780 ms dropped cold, 240 ms warm** — both inflated by about 19%, because they were
> counted above a resampler that was emitting 1.19x its input until 2026-09-07
> ([ADR 0020](docs/adr/0020-capture-settling.md)); no constant depends on them. The remaining +30 dB was found in `Internal Mic
> Boost` rather than the `Capture` control ADR 0019 names, and has been turned off on that
> machine. **Live speech was recognised for the first time on 2026-09-06**: thirty seconds
> read aloud, transcribed substantially correctly and translated, with nothing dropped and
> the input measuring `ok (peak 0.63, rms 0.066, floor 0.015)` — squarely inside the
> recorded-speech range. It also exposed the run-on utterance described below.

## What it is meant to be

A translator that runs entirely on your own machine. You speak, it recognises, it
translates, it shows you the result — and nothing you said leaves the device or outlives
the conversation by more than ten seconds.

Three constraints shape everything:

**It runs locally.** Not for speed — because a promise about your speech being deleted is
only worth making if it can be kept, and that stops being true the moment audio is handed
to someone else's server. See [ADR 0001](docs/adr/0001-on-device-inference.md).

**It stays free.** No paid API, no metered service, no free tier that can be withdrawn
later. Local models have no per-request cost and no vendor who can change the terms.

**It forgets.** Transient content — audio, transcripts, translations — lives ten seconds
past its last use and is then deleted, automatically. Live translation is fine; scrollback
and history are deliberate exceptions with owners and expiry dates, not defaults. See
[docs/RETENTION_POLICY.md](docs/RETENTION_POLICY.md).

## Plan

**Phase 1 — desktop.** Windows, macOS, Linux. Python, on-device pipeline, PySide6
interface. Proves the pipeline and the retention module against real audio.

**Phase 2 — mobile.** iOS and Android. A port of the edges, not a rewrite of the core
([ADR 0002](docs/adr/0002-desktop-first-delivery.md)).

[ADR 0017](docs/adr/0017-any-hardware.md) surveys what that actually costs. Every dependency
has ARM builds, so **ARM Linux and Apple Silicon should work today** — untested, because no
ARM hardware was available, and a wheel existing is not the same as it running. Phones are a
different question: the recogniser ships Android artefacts, but CTranslate2 has no mobile
support, so mobile translation goes through ONNX Runtime — already a dependency here, with
official Android and iOS builds — rather than a new library.

**That second engine now exists** ([ADR 0018](docs/adr/0018-onnx-translation.md)):
`--translation-engine onnx` runs the same model through ONNX Runtime, behind the same port,
with nothing above `infrastructure/` aware of the difference. It is 2.4–2.7x slower on this
desktop, so CTranslate2 stays the default — the portable engine is for hardware where the
choice is not between two engines but between one and none. **Nothing has been run on a
phone**; capture and the interface are still desktop-only.

## The window

```bash
pip install -r requirements-ui.txt
python -m on_the_fly gui
```

A dark caption window: what is being said in white, the translation under it in green,
partials dimmed so "this may still change" is visible without a word for it.

**Its language pickers now offer only what is pinned.** They were built from the tier table
in `domain/languages.py`, which at the time marked all seven languages `STREAMING` because a
published model existed for each — so the window offered seven sources and seven targets,
forty-nine pairs, and could serve two. Picking German got you through `STARTING` and `loading
recognition model` to a raw `KeyError` repr, after the pickers had already promised the
pair. The command line has always refused the same request before opening a device, with a
sentence saying what is missing; `src/on_the_fly/app/catalogue.py` now derives the window's
offer from the same pin registries, so the source list is whatever has a pinned model — three
languages today — and the target list follows the source rather than sitting fixed beside
it. French was added afterwards and needed no change here, which is the point of deriving it —
and so was the bridged `fr↔ru` pair, which appears in the target list because the catalogue
asks whether a pair can be *served*, not whether one artefact serves it.

The target picker also offers *no translation* — live captions in the language being spoken,
which the pipeline has always supported. Its row deliberately carries no language code.
Using the source's own code was the obvious choice, since a view state encodes "not
translating" as target == source, and driving the window found what it costs: under an
English source `ru` means *into Russian*, under a Russian source the same code means *not at
all*, so switching the source language silently stopped translating while both pickers still
looked right.

**It tells you when your microphone is unusable**
([ADR 0019](docs/adr/0019-input-levels.md), [ADR 0021](docs/adr/0021-too-loud-input.md)).
Bad input does not produce silence or an error — it produces fluent words nobody said, in a
language you probably cannot check. So the window says *the microphone is far too loud —
turn its input gain down*, and `stream` and `listen` print the same for a recording. The
readings are five numbers and no audio.

The measurement that decision was calibrated against was wrong, and
[ADR 0020](docs/adr/0020-capture-settling.md) corrects it in place: the **51% of samples at
full scale** ADR 0019 attributes to the reference machine's microphone was the capture path
powering up. Settled, at the very same mixer settings, it clips 0.03% of its samples while
running five to eight times hotter than recorded speech — a condition the clipping check was
never going to catch, because it is not a clipping problem.

[ADR 0021](docs/adr/0021-too-loud-input.md) then measured what over-gain actually costs and
found the obvious reading wrong: speech with **a fifth of its samples pinned at full scale
still transcribes word for word**. What broke recognition was an amplified empty *room*, and
peak, rms and crest factor cannot tell the two apart — they are numerically identical. The
difference is over time: speech has pauses and a room does not. So the check that catches this
measures the **quietest tenth of the last five seconds**. Replayed against the gain the
reference machine was found at, that check reports
`too_loud (peak 1.00, rms 0.445, clipped 0.9%, floor 0.373)` where the product used to print
`input ok` beside two words transcribed from an empty room. Note the 0.9%: lowering the
clipping threshold would never have found it. Thirty seconds of live speech at the corrected
gain measures a floor of 0.015 against a threshold of 0.15, so the margin is an order of
magnitude — from one speaker, one room and one microphone.

## Listening, without a window

```bash
python -m on_the_fly listen --seconds 15 --translate-to ru
```

The same streaming pipeline as `stream`, reading a microphone instead of a file. It runs
until Ctrl-C or `--seconds`, and it is the only way to exercise the capture path without a
GUI toolkit installed — which is how both of the findings above became visible.

```text
language      English (en, streaming)
model         streaming-en (local, verified, Apache-2.0)
model load    5.09s

  listening (8s)

device        captured at 16000 Hz
settling      240ms discarded, before the input steadied
audio         7.66s in 383 frames
wall time     8.01s
dropped       none - nothing was lost to a slow pipeline
events        0 partial, 0 final
input         ok (peak 0.09, rms 0.016, clipped 0.0%)
retention     clean - nothing retained, no deletion failed
```

No real-time factor: live audio arrives in real time by definition, so the ratio is always
about 1.0 and says nothing. `dropped` is the live equivalent — overflows are words the
pipeline was too slow to receive.

**That run is a silent room, and it used to be the failing half of a pair.** At the mixer
setting this machine was found in, the same eight seconds of silence transcribed as `IN` and
`EVERY` — two finalised words, from noise, with `input ok` printed beside them.

It now says `too_loud` ([ADR 0021](docs/adr/0021-too-loud-input.md)), and the reason it did
not is worth more than the fix. **Clipping was the wrong thing to measure.** Recorded speech
amplified until 21% of its samples sit at full scale still transcribes *word for word* — the
recogniser barely minds distortion. What invents words is an amplified room: a noise floor
lifted to speech-like energy with nothing being said in it. The two are identical in peak,
rms and crest factor, so nothing instantaneous separates them:

| | peak | rms | crest | outcome |
| --- | --- | --- | --- | --- |
| speech at 12x gain | 1.000 | 0.420 | 2.4 | transcribes perfectly |
| an empty room at +60 dB | 1.000 | 0.409 | 2.4 | invents words |

What separates them is time: **speech has pauses and a room does not.** So the check is now
the quietest tenth of the last five seconds — 0.122 for speech amplified 24x, 0.333 for that
room — with the line at 0.15, which is where measured word error starts. Five seconds
because at one second the two are indistinguishable.

**There is no scrollback, on purpose** ([ADR 0016](docs/adr/0016-desktop-interface.md)). The
window shows one utterance; the next one replaces it. `docs/RETENTION_POLICY.md` puts it in
a line — *live translation is fine, scrollback is not* — and a history pane would quietly
break the promise the project is arranged around. The cost is real and worth stating: look
away and you miss it. Adding history later needs a record in `docs/EXCEPTIONS.md` with an
owner and an expiry, which is the right amount of friction for a change to what the product
retains.

PySide6 is an **optional extra**. The pipeline, the command line and the whole test suite run
without a GUI toolkit installed.

**`input ok` does not mean recognition is working**
([ADR 0026](docs/adr/0026-accuracy-and-level.md)). Attenuating a voice against this room's own
recorded noise, the recogniser stays word-perfect to +1 dB SNR, degrades gently to −10 dB, and
below about −14 dB emits **nothing at all** rather than inventing words — the opposite of the
over-gain failure, and the safer of the two.

Across that entire range, from word-perfect to recognising nothing, the measured level travels
from `rms 0.056` to `0.031`. Once the voice is quiet, the room is what is being measured. No
statistic this project computes separates the working rows from the failing ones — `peak/floor`
is not even monotonic — so **there is deliberately no verdict for quiet or distant input**. A
threshold placed anyway would fire on inputs that work and stay silent on inputs that do not.

The same measurement undermined a tidier story: a garbled live run implied about +3 dB SNR,
which that table puts at 0% word error. **It was the room**
([ADR 0027](docs/adr/0027-reverberation.md)). Convolving the same speech with validated
synthetic impulse responses, at constant level so reverberation is the only variable:

| RT60 | distance | DRR | reverb only | reverb + room noise |
| --- | --- | --- | --- | --- |
| 0.44 | 0.5 m | +1.2 dB | 2.1% | 0.0% |
| 0.44 | 1.0 m | −4.9 dB | 4.2% | 18.8% |
| 0.70 | 1.0 m | −10.3 dB | **52.1%** | **95.8%** |
| 0.69 | 2.0 m | −13.1 dB | 45.8% | 87.5% |

There is a cliff between about −9 and −10 dB DRR, and the two mechanisms **compound rather
than add** — noise alone costs nothing at this level, reverberation at 1 m in a quiet room
costs 4.2%, and together they cost 18.8%. A normally furnished room with a laptop at arm's
length sits close to the edge of that cliff.

**Sit closer to the microphone, or use a headset.** From 2.0 m to 0.5 m is 87.5% word error
down to 37.5% in a live room, and 27.1% down to 0.0% in a quiet one — nothing else available
comes close. The application does not say so, because DRR cannot be derived from anything it
measures: the reverberant files are *quieter* than the ones that work, and every row above
reads `input ok`.

**Dereverberation was measured and not adopted**
([ADR 0030](docs/adr/0030-dereverberation.md)). WPE is the standard fix, and single-channel
WPE was implemented and tuned to find out whether it was worth a dependency. It helps
consistently — 87.5% word error becomes 41.7% at its best setting — and never damages input
that already works, so it would need no detector. It was still rejected: it **rescues
nothing**, moving no broken case below the 15% this project treats as usable; the best setting
costs **0.84x real time** on top of recognition's 0.399x, against a latency budget already at
its target under load; and the measurement is offline with full lookahead, so anything live
would do worse. The same room at 0.4 m instead of 1.0 m measures 2.1% against 87.5%, for free.

**The reference room has now been measured** ([ADR 0029](docs/adr/0029-measured-room.md)).
Four averaged sine sweeps put its RT60 at roughly **0.6–0.9 s** — a live room, in the half of
the grid above where the cliff sits. At 0.4 m that predicts 2.1% word error and at 1.0 m
35.4%, which is the range the live runs in this project actually produced. The first fit gave
1.32 s and was wrong: the Schroeder curve stops decaying at the measurement noise floor, and
fitting past it measures the noise.

What that measurement *cannot* say is anything about distance. A laptop's speaker sits a
hand's width from its microphone, so 91.5% of the energy arrives within 5 ms and speech
through that exact path scores 2.1% — nothing like a person a metre away. RT60 transfers
because it belongs to the room; the direct-to-reverberant ratio does not.

The obvious way to detect it was built and rejected
([ADR 0028](docs/adr/0028-blind-reverberation-estimate.md)). Reverberation fills the gaps
between words, so `floor/rms` should rise with it — and across thirty-four synthetic mixtures
it did, separating broken from usable with no missed detections. On five real recordings it
inverted: the worst run scored *lower* than the best, and an empty room scored highest of all,
because a statistic measuring energy in the gaps cannot tell a live room from a silent one.
The synthetic set had exactly one thing wrong with it — every sample contained speech.

**The voice detector used to deafen itself** ([ADR 0025](docs/adr/0025-vad-noise-floor.md)).
Its noise floor adapted only on frames it called silence — so a frame it *missed* counted as
silence, lifted the floor, and made the next miss more likely. Sixteen seconds of somebody
not pausing was enough, and starting the application mid-sentence seeded the floor on speech
and made it deaf from the first frame. Scored against a reference labelling it managed 13–32%
where it now manages 93–96%. The floor now falls fast and rises slowly, which is the whole
fix. It affects `segment` and `transcribe`; the live path uses the recogniser's own
endpointing and never touched it.

## Repository layout

| Path | Contents |
| --- | --- |
| `CODING_AGENT_*`, `REPOSITORY_GOVERNANCE_*` | The normative policy stack |
| `docs/` | Security, retention, governance, performance, exceptions, ADRs |
| `scripts/` | Validators that enforce the policy stack in CI |
| `tests/` | Tests for those validators |
| `src/on_the_fly/domain/retention/` | The ten-second rule, enforced at runtime |
| `src/on_the_fly/domain/audio/` | Capture, voice activity detection, utterance segmentation |
| `src/on_the_fly/infrastructure/audio/` | Microphone and WAV adapters — the only place PortAudio exists |
| `src/on_the_fly/infrastructure/model_store.py` | Model pins and digest verification, for both engines |
| `src/on_the_fly/infrastructure/asr/` | Pinned models and the Whisper recogniser |
| `src/on_the_fly/infrastructure/translation/` | Pinned translation artefacts and both translators, CTranslate2 and ONNX Runtime |
| `src/on_the_fly/app/` | Composition root and command line |
| `src/on_the_fly/ui/` | The desktop window — logic in `caption.py`, widgets in `window.py` |

## Try it

```bash
python -m on_the_fly segment recording.wav
```

```text
file          recording.wav
format        16000 Hz mono 16-bit
audio         3.90s in 195 frames

2 utterance(s):
  #1   start=   0.00s duration= 2.00s frames=100   ended=SILENCE
  #2   start=   2.10s duration= 1.70s frames=85    ended=SILENCE

wall time     0.050s
real-time     0.0128x  (segmentation only)
invalid       0 frame(s)
retention     clean - nothing retained, no deletion failed
```

Mono 16-bit WAV; the file is not resampled. `--json` gives the same thing machine-readably,
and `--allowed-root` confines the input path when it comes from somewhere less trustworthy
than your own shell.

A WAV header states how much audio the file holds, and a recorder that crashed or a copy
that stopped leaves that claim overstated. Every duration above is measured from the audio
that arrived, so none of them can notice — and half a recording transcribes into a perfectly
well-formed sentence. When the file is short of what it declares, a `truncated` line says so.

To stream English — text appears while the speaker is still talking:

```bash
python -m on_the_fly stream recording.wav --allow-download
```

```text
file          recording.wav
language      English (en, streaming)
model         streaming-en (local, verified, Apache-2.0)
model load    2.31s

  [   0.00s partial] AFTER
  [   0.00s partial] AFTER EARLY
  [   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP

audio         6.62s in 331 frames
wall time     2.64s
real-time     0.399x  (keeps up)  excludes model load
first text    1.10s into the audio
events        16 partial, 1 final
retention     clean - nothing retained, no deletion failed
```

**That run is from an idle machine, and it predates a bug in the last word.** The clip
continues `...HERE AND THERE THE SQUALID QUARTER OF THE BROTHELS`, and until 2026-09-06 the
final ended at `BROTHEL`. A transducer emits a symbol only once it has frames after it, and
when audio simply stops there are none — `input_finished()` does not supply them — so the
last word of every stream came out truncated or not at all. `finish()` now feeds the decoder
silence before closing the stream, which recovers it. Over the English model's own published
test set that is **3.0% word error against 0.0%**: two files, two lost words, on the pin this
project measures everything else against.

**The length was wrong at first.** It was set at 500 ms from English and French, which need
300 ms and 100 ms — "300 plus margin for a model neither of these measured". The model
neither of them measured was Russian, which needs **1000 ms**, so for a day every Russian
utterance ending a stream quietly lost its tail. It is now 1.5 s: margin over a measurement
rather than over a guess.

The tail is silence the recogniser makes up, so two things are checked rather than assumed.
It decodes to nothing on its own — three seconds of digital zeros stays empty on all three
pinned models at every tail length tested, which is the failure mode
[ADR 0021](docs/adr/0021-too-loud-input.md) exists to guard against. And it is not counted as
audio that arrived, so no duration or real-time factor is inflated by it. What it does cost
is 0.2 to 1.3 s of decoding at the end of a stream, once, which the `wall time` above does
not include.

`scripts/measure_recognition.py` is the tool that found it: point it at a model directory
and a folder of wavs with a reference transcript, and it reports real-time factor and word
error rate the way the pipeline decodes — 20 ms frames, decoding between them, rather than a
batch pass that would flatter a model that cannot keep up.

Only languages with a pinned streaming model are accepted. A language without one is
refused rather than silently downgraded to batch latency — being told "no, use transcribe"
is better than wondering why it is slow.

French streams the same way, and stops there:

```bash
python -m on_the_fly stream recording.wav --language fr --allow-download
```

```text
language      French (fr, streaming)
model         streaming-fr (local, verified, Apache-2.0)

  [   0.00s partial] CE DERNIER ÉVOLUE TOUT AU LONG DE L'HISTOIRE RO
  [   0.00s final  ] CE DERNIER ÉVOLUE TOUT AU LONG DE L'HISTOIRE ROMAINE

audio         3.80s in 190 frames
wall time     4.33s
real-time     1.140x  (TOO SLOW)  excludes model load
first text    1.06s into the audio
events        9 partial, 1 final
retention     clean - nothing retained, no deletion failed
```

**That `TOO SLOW` is left in because it is what the command printed.** It is a 3.8-second
clip on a machine at load average 7, which is a measurement of the machine and of the fixed
cost of one short stream, not of the model — the six paired runs above, over 23 seconds of
identical audio, put French at 0.868x against English's 0.804x under the same conditions.
Both models cross 1.0 on this machine when it spikes. The honest summary is that French
costs about what English costs, and that neither has much headroom on a laptop doing other
things.

And with `--translate-to en` it finishes the job:

```text
  [   0.00s final  ] CE DERNIER ÉVOLUE TOUT AU LONG DE L'HISTOIRE ROMAINE
           → The latter evolves throughout Roman history
```

Two measurement scripts back those numbers, and both are in `scripts/`.
`measure_recognition.py` decodes a folder of wavs the way the pipeline does — 20 ms frames,
decoding between them, rather than a batch pass that would flatter a model that cannot keep
up — and reports real-time factor and word error rate. `measure_translation.py` translates a
publisher test set through the shipped translator and reports chrF2 and latency; its chrF2 is
written out rather than pulled in from `sacrebleu`, and checked against Helsinki-NLP's own
published figure before being trusted — **66.95 where they publish 66.9**.

**Greedy decoding is not free, and the documents used to say it was.** ADR 0009 measured one
direction at 300 sentences, found greedy indistinguishable from the publisher's beam 6, and
that finding got quoted. At 1000 sentences every direction pays something — 0.26 chrF2 for
`en→ru`, up to **1.39 for `fr→en`**. Greedy is still what ships, because beam 6 adds 400–580
ms to every final against a latency budget already at its target under load. It is a trade,
not a free lunch, and [docs/PERFORMANCE_BUDGET.md](docs/PERFORMANCE_BUDGET.md) now says so.

Add `--translate-to ru` and finalised text is translated as well:

```text
translation   opus-mt-en-ru (local, verified, CC-BY-4.0)
attribution   English-Russian translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-11,
              licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT

  [   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE
           → После раннего наступления темноты, желтые лампы загорались здесь и там

translation   1 of 1 final(s), median 1476ms, max 1476ms
```

**It meets its latency budget on an idle machine and sits on the line under heavy load.**
Over 65 utterances, with three of four cores deliberately busy for the loaded column:

| | idle | 3 of 4 cores busy | target |
| --- | --- | --- | --- |
| Endpoint → caption p50 | **332 ms** | **710 ms** | 700 ms |
| Endpoint → caption p95 | **736 ms** | **1662 ms** | 1500 ms |
| Endpoint → caption p99 | 944 ms | 2219 ms | 4000 ms (hard) |

Ten milliseconds over target at p50 under load is within these measurements' variance — *at
the line* is the honest description. A live translator runs on a laptop while its user is
doing other things, so the loaded column is the one that matters.

Decoding is greedy rather than the publisher's beam 6. Measured on Helsinki-NLP's own test
sets against their human references, that costs nothing detectable in either direction —
chrF2 66.62 against 66.56 for `en→ru`, 73.17 against 72.73 for `ru→en` — and runs 2.3–2.4x
faster. Those four figures come from a 300-sentence sample whose exact membership was not
recorded and do not reproduce from the first 300; measured again at 1000 sentences, greedy
costs 0.26 chrF2 for `en→ru` rather than gaining
([ADR 0032](docs/adr/0032-french-translation.md)). The decision stands on latency: beam 6 is
1.5x to 3.3x slower per sentence against a budget already at its target under load.

Translation is also bounded to one thread. CTranslate2 defaults to using every core, which
measures **6.9x slower under load** — 2899 ms against 421 ms with three of four cores busy —
because a single translation taking every core gets descheduled. One thread costs about 10%
on an idle machine and is the only setting that meets the budget on a busy one.

See [docs/PERFORMANCE_BUDGET.md](docs/PERFORMANCE_BUDGET.md).

The other direction streams too ([ADR 0012](docs/adr/0012-russian-streams-after-all.md)):

```bash
python -m on_the_fly stream recording.wav --language ru --translate-to en --allow-download
```

`transcribe --translate-to` remains available for any language without a streaming pin.

**Utterances end where you pause** ([ADR 0024](docs/adr/0024-trailing-silence.md)), with an
eight-second ceiling for a speaker who does not
([ADR 0022](docs/adr/0022-endpoint-ceiling.md)).

Both of those were wrong until today. The first live speech this project ever recognised came
back as two finals, one of them fourteen seconds long, translated in a lump after the speaker
stopped. Two causes, found in that order:

`rule3_min_utterance_length=300` was a value in seconds written as though it counted frames,
so the ceiling was five minutes and could never fire. And the rule that was left doing all
the work — 1.2 s of trailing silence — turned out to fire on **two of seventy-five** pauses
in thirty seconds of measured conversational speech. It was effectively switched off.

Real pauses are two populations: gaps inside speech at 0.12–0.22 s, then sentence boundaries
from about 0.56 s. The threshold is now 0.5 s, in the space between them.

Those percentiles now carry a caution ([ADR 0024](docs/adr/0024-trailing-silence.md)): they
were measured through a microphone on a machine whose inputs refuse 16 kHz, so the audio was
resampled — and the resampler was emitting 1.19x what it was given until 2026-09-07. The
threshold stands because it was confirmed against live speech afterwards; the exact
percentiles do not.
`scripts/measure_pauses.py` derives those numbers from a live microphone and keeps no
audio — durations only — which is what makes the parameter tunable without recording anyone.

**Confirmed live:** ninety seconds of speech produced sixteen utterances, **fourteen ended by
a pause** and two by the ceiling, at a median of about 5.3 s each. Translation latency fell
to a median of 188 ms from 553 ms, because the translator now receives sentences rather than
eight-second lumps. (The pause table predicted 3.3 s; it counts energy gaps, while the
recogniser measures silence inside its decoder, so it under-predicts. Direction right,
magnitude optimistic.)

It also removed most of the ceiling's damage. The ceiling cuts wherever the clock lands,
including inside a word, and three copies of the test sample used to come out as 20, 22 and
13 words with `BROTHEL` split across two utterances. They now come out as three whole
18-word sentences, and the ceiling never fires at all.

**Partials are never translated** ([ADR 0009](docs/adr/0009-translation.md)). Translating
text that is about to be revised costs an inference per partial — sixteen on the sample
above — and produces a caption that rewrites itself. So the source caption streams and the
translation arrives when the speaker stops. That is prompt translation, not live
translation, and it should not be described as the latter.

The model is Helsinki-NLP's own OPUS-MT release, pinned by URL and SHA-256 and converted
locally; the conversion is a derived cache and is never what gets trusted. Its licence is
CC-BY-4.0, which requires attribution, which is why the attribution line is printed rather
than buried in a source file.

**A second engine, for hardware CTranslate2 cannot reach** ([ADR 0018](docs/adr/0018-onnx-translation.md)):

```bash
python -m on_the_fly stream recording.wav --translate-to ru --translation-engine onnx
```

Same model, same port, ONNX Runtime instead — the runtime with official Android and iOS
builds. **Both pinned pairs run on both engines.** Measured against the default on 300
sentences of each publisher test set:

| | CTranslate2 | ONNX |
| --- | --- | --- |
| `en→ru`, chrF2 vs human references | **66.62** | 66.33 |
| `ru→en`, chrF2 vs human references | **73.17** | 72.59 |

Roughly **two to three times slower** — the ratio ranged 1.6–3.0x across runs, and a single
decimal would be claiming precision these conditions do not support. The quality gap is the
export rather than the quantisation: the full-precision graphs score the same for 232 MB
more.

Measuring the second direction is what caught a defect in the first: OPUS-MT shares one id
between padding and the decoder start token, and the publisher's `bad_words_ids` forbids
generating it. One sentence in 300 emitted `<pad>` until the token budget ran out — 9.7 s of
work for nothing. `en→ru` never hit it, which is the argument for covering both directions
rather than sampling one.

Its artefact is a third-party conversion, since Helsinki-NLP publish Marian weights and not
an ONNX export. ADR 0009's rule is *pin what the publisher published, never a conversion*,
so the converter is admitted under Article 12 with its own review rather than the rule being
quietly bent.

For the batch engine, first fetch the pinned model (78 MB, once):

```bash
python -m on_the_fly transcribe recording.wav --allow-download
```

```text
file          recording.wav
model         base (local, verified)
audio         3.90s

  [   0.00s +2.00s] good morning, how are you
  [   2.10s +1.70s] very well thank you

wall time     8.94s
recognition   8.90s of that
real-time     2.29x
```

The model is pinned by revision and SHA-256 in
[`models.py`](src/on_the_fly/infrastructure/asr/models.py) and verified on every load. A
file that does not match is refused and left in place for inspection — never silently
re-downloaded. Downloading is off unless you ask for it.

The last line is the one worth reading. Every run states whether it finished holding
nothing, and exits non-zero if it could not delete what it held.

## Working here

```bash
make check
```

On Windows, where `python` is often a Store stub:

```bash
make PYTHON=py check
```

That runs the same gates as CI, in the same order: policy validation, governance
validation, lint, type check, tests.

Start with [docs/CODING_AGENT_ADOPTION.md](docs/CODING_AGENT_ADOPTION.md). It explains the
policy stack, what it enforces, and — more usefully — what it does not yet enforce.

The rule worth internalising before anything else: do not state that a file, test, control,
branch rule, or CI result exists or passed unless you have inspected or executed it.

## Licence

Apache-2.0. Model weights carry their own licences and are reviewed individually; several
widely used multilingual models are non-commercial and are excluded for that reason.
