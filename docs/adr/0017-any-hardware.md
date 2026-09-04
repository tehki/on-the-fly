# ADR 0017 — What "runs on any hardware" costs, and the one dependency that blocks it

**Status:** Accepted as a finding. **No port is started here.**
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** LOW as committed — this is a survey and a plan, no code changes

## Context

The requirement is to run on any hardware, phones included. ADR 0002 put mobile in phase 2
and said it would be "a port of the edges, not a rewrite of the core".

Before writing any of that port, the cheap question: **what already runs where?** Checked
against real package indexes and release artefacts rather than assumed, because the last
several findings in this project came from checking a claim nobody had verified.

## "Any hardware" is two different questions

The first thing the survey settled is that the phrase hides a split, and the two halves have
very different answers.

### ARM Linux and Apple Silicon: works today

| Dependency | aarch64 wheels |
| --- | --- |
| `sherpa-onnx` | yes — `manylinux2014_aarch64`, `macosx_11_0_arm64`, even `linux_armv7l` |
| `ctranslate2` | yes — `manylinux_2_28_aarch64`, `macosx_11_0_arm64` |
| `sentencepiece` | yes |
| `numpy` | yes |
| `av` | yes — including `armv7l` |
| `onnxruntime` | yes |

Every runtime dependency has ARM builds. A Raspberry Pi, an ARM server, an Apple Silicon
Mac: the pipeline should install and run unchanged, and single-threaded inference
(ADR 0014) is what those cores want anyway.

**This is a claim about wheel availability, not a test.** No ARM hardware was available, so
nothing here was executed on one. It is the difference between "there is a wheel" and "it
works", and this project has been caught by exactly that gap before.

### Android and iOS: a different problem entirely

Those wheel tags do **not** mean phones. `manylinux_aarch64` is glibc; Android is bionic.
`macosx_arm64` is a Mac, not an iPhone. Checked per component:

| Component | Android | iOS |
| --- | --- | --- |
| **sherpa-onnx** (recognition) | **yes** — ships an official `.aar` in every release | not in the release assets |
| **CTranslate2** (translation) | **no** — three open unresolved issues, including "Build CTranslate2 for android" and a compilation failure report | nothing |
| **PySide6** (interface) | no | no |
| **sounddevice / PortAudio** (capture) | no — Android uses AAudio/OpenSL | no — iOS uses AVAudioEngine |

## The finding: one dependency blocks the pipeline, and it is not the obvious one

Recognition was the part that looked hard — it is the expensive stage, and ADR 0008 chose
sherpa-onnx partly *because* it targets mobile. That bet paid: sherpa-onnx ships Android
artefacts as a first-class output.

**Translation is the blocker.** CTranslate2 has no mobile story, and the issues asking for
one have been open without resolution.

## The way through: converge on ONNX Runtime

`onnxruntime` is **already a dependency of this project**, arriving under sherpa-onnx, and
it has official mobile builds:

```text
Android   com.microsoft.onnxruntime:onnxruntime-android   1.29.0   (Maven Central)
iOS       onnxruntime-c                                            (CocoaPods)
```

1.29.0 is the same version already installed here.

So the phase-2 shape is not "find a mobile translation library". It is: **run both models on
the runtime the recogniser already uses.** OPUS-MT exports to ONNX; a second `Translator`
implementation runs it through `onnxruntime` instead of CTranslate2; the pipeline above does
not change, because `Translator` is a port and always was.

That is what ADR 0002 meant by porting the edges, and it is the first time the claim has been
tested against a concrete obstacle rather than asserted.

### What that path costs

- **An ONNX export of OPUS-MT**, pinned and digest-verified like every other artefact — and
  subject to the same rule ADR 0009 established: *pin what the publisher published, never a
  conversion*. An export this project performs is an artefact this project publishes, with
  the obligations that carries.
- **A second translator implementation**, kept behind the existing port so desktop can keep
  CTranslate2 — which is measurably fast (ADR 0014) and has no reason to change.
- **Measuring it.** ONNX Runtime and CTranslate2 will not perform identically, and this
  project does not adopt performance claims without evidence.

### What it does not cost

The domain, the retention module, the segmenter, the streaming port, the language registry
and the pipeline. None of them import an inference library, which is the point of the
architecture and the reason this survey came out as well as it did.

## Two things needing replacement regardless

- **Capture.** PortAudio is not the mobile audio layer. `AudioSource` is a port with two
  implementations already (microphone and WAV), so a third is the intended shape rather than
  a surprise — and ADR 0015's callback design is closer to how AAudio and AVAudioEngine work
  than blocking reads were.
- **The interface.** PySide6 does not run on phones, and ADR 0016 recorded that a phone
  translator is a full-screen caption rather than a window, so the interface would be
  rewritten for the form factor even if the toolkit ran.

## Decision

**Record the finding; start no port.** Specifically:

1. ARM Linux and Apple Silicon are treated as supported-in-principle and **untested** until
   someone runs them.
2. Mobile translation goes through ONNX Runtime, not CTranslate2, when it is built.
3. CTranslate2 stays the desktop translator. It is fast and measured, and nothing about
   mobile is a reason to make the desktop worse.

## Review trigger

When phase 2 starts, or if CTranslate2 gains mobile support — that would remove the only
reason to maintain two translator implementations, and is worth watching for rather than
assuming.
