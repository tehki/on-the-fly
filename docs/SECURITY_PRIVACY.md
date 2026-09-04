# Security and privacy

The governing rules are in `CODING_AGENT_CONSTITUTION_v1.2-otf1.md` and
`CODING_AGENT_POLICY_v1.2-otf1.yaml`. This document says what they mean for a live translator
specifically, and what is and is not built yet.

## Current status

Application code exists. This section describes what it enforces at runtime, established
by reading the tree rather than by intent, because a status section that has fallen behind
the code is the exact failure Article 2 and handbook 52 exist to prevent.

**Enforced now:**

| Control | Where | What that means concretely |
| --- | --- | --- |
| Ten-second post-use expiry, driven by a scheduler | `app/pipeline.py` builds an `EphemeralStore` and runs a `ThreadedReaper` for every run | Captured audio frames (`domain/audio/segmenter.py`) and recognition transcripts (`app/pipeline.py`) are held *through* the store rather than beside it |
| Deletion failure is reported, never swallowed | `app/cli.py` | Every command ends by stating whether the run finished holding nothing, and returns a distinct exit code when it could not delete what it held |
| Model weights verified before use | `infrastructure/asr/model_store.py` | A model declaring no digests refuses to load; a file whose SHA-256 does not match its pin is refused and left in place for inspection; `allow_download` defaults to false |
| The microphone is acquired late and released on every path | `infrastructure/audio/microphone.py` | Nothing is opened at construction, and device names are kept out of `repr` |

**Not built, and therefore claimed by nothing:**

- **No translation.** `Translator` in `domain/audio/ports.py` is a port with no
  implementation.
- **No `Deleter` for a real spill location.** Nothing spills to disk yet, so the store is
  constructed with an empty deleter list and only process memory is purged.
- **`OPERATIONAL_METADATA` has a constant and no storage behind it.** The counters that
  exist are in-memory and metadata-shaped; nothing is written anywhere for 30 days.
- **Nothing is encrypted.** Constitution Article 16 is a floor this project has not yet
  had to stand on, not a description of it.
- **The microphone adapter has captured real audio, but not *usable* audio.** It has now
  been run against a real device: frames arrive, the device is released on every exit path,
  and real driver failures map to typed errors. The test machine's input was saturated and
  DC-offset — raw `sounddevice` behaved identically, so that is the hardware — which means
  recognition from a live microphone is still unverified. The privacy-relevant behaviour
  *was* verified: nothing is opened at construction, the device is released on exit, and
  the `repr` carries no device name.

When any of that changes, this section changes with it.

## What this application will handle

Speech. That is a category of data people are careless with precisely because it feels
transient: medical conversations, legal matters, family arguments, business negotiations,
things said in a language the speaker assumes the room does not understand. A translator
sits in the middle of all of it.

Two consequences drive the design.

### Local-first inference is a privacy decision, not a performance one

Constitution Article 6 gives transient content a ten-second post-use lifetime, and
Article 8 requires that deletion actually happen everywhere. Neither obligation can be met
for data sent to a third-party cloud API: once audio leaves the device, its retention is
governed by someone else's policy, and any deletion claim would be unverified — which
Article 8 invariant 10 forbids stating.

So the default is on-device recognition and translation. A remote service may be offered
only as an explicit, off-by-default, per-session choice, with the user told plainly that
their audio leaves the device and that this repository's retention guarantees stop at the
boundary.

### Nothing is stored that is not needed to translate the sentence in flight

See `docs/RETENTION_POLICY.md`. Live translation refreshes the retention window as it
runs; scrollback, history, replay, and export retain content past it and each require a
recorded exception.

## Invariants as they apply here

| Invariant | What it means for this project |
| --- | --- |
| Unauthorised input cannot trigger a protected side effect | Microphone capture, network egress, and file writes are all authorised actions, not incidental ones. |
| Secrets never enter source, logs, telemetry or diagnostics | Includes any API key for an optional remote engine. Never in a crash report or a screenshot. |
| Transient content does not outlive its deadline | Enforced by a scheduler, not by remembering to clean up. Deletion failure is reported as a security event. |
| Untrusted input cannot become code, a command, a path, or a network target | Recognised speech is untrusted text. It is never a shell argument, a path component, a template, or a URL. A model file path is validated against an allowed root. |
| TLS verification is never disabled | Applies to model downloads as much as to any optional translation service. |
| Security failures fail closed | A recogniser that cannot verify a model, or a retention scheduler that cannot delete, stops. It does not continue quietly. |
| Dependency trust is not implicit | Model weights are dependencies. See below. |
| No unverified security claims | This repository does not describe audio as encrypted, deleted, or isolated unless that has been verified. |

## Device access

Opening a microphone is the most privileged thing this project does, and it is the one the
user can see — the indicator light on their machine is part of the interface whether we
intend it or not.

`MicrophoneSource` therefore opens nothing at construction. The device is acquired when
capture starts and released on every exit path: normal end, exception, and a caller that
stops consuming. Holding a microphone open while idle is a privacy problem even if nothing
reads from it.

Device names are metadata, but not innocuous ones: people name hardware after themselves,
so "Ilya's AirPods" identifies a person. Device names may be shown to the user who owns the
device. They must not enter logs, metrics, telemetry or crash reports, and the adapter's
`repr` deliberately omits them.

The PortAudio binding ships prebuilt native binaries on Windows and macOS. That exposure is
recorded in ADR 0003 rather than glossed over; it is mitigated by a pinned version and
published build provenance, and it is explicitly **not** a reproducible-build claim.

## Model weights are dependencies

A speech or translation model is executable trust in the same way a package is: it is
downloaded, it is large, it is opaque, and it shapes output. Article 12 applies to it.

Each model admitted must record its source and publisher, its licence and whether that
licence permits this project's use, its integrity reference (a checksum pinned in the
repository, verified after download), where it is cached and under which retention class,
and what happens when verification fails — which is: refuse to load it.

Licences deserve particular attention here. Several of the strongest multilingual
translation models are released under non-commercial terms, which is a licence
incompatibility rather than a technical one, and it will not be discovered by any scanner.

## Transport claims

The inherited policy stack carries detailed rules about Telegram transport security. This
project has no Telegram component, so those rules are inert here — but the general
invariant behind them is not, and it is the one that matters:

> A stated security property must match the transport actually verified to be in use.

Running a component locally is not encryption. TLS to a service is not end-to-end
encryption. An application-layer HMAC is authentication and integrity, not confidentiality.
If this project ever describes a call as private, encrypted, or peer-to-peer, that claim
must name the mechanism and be verifiable.

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue. Do not
include audio samples, transcripts, credentials, or other sensitive content in a report —
a reproduction case with the content removed is more useful and does not create a second
disclosure. Handbook 60 governs the response: contain, preserve minimum
evidence, rotate anything exposed, fix the root cause, add a regression test.
