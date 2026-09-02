# ADR 0003 — `sounddevice` as the microphone binding

**Status:** Accepted
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — first runtime dependency, and it carries prebuilt native binaries

## Context

`src/on_the_fly/domain/audio/` implements capture, voice activity detection and utterance
segmentation, but every `AudioSource` so far is a test fixture. Nothing has ever heard a
microphone. Connecting one needs a binding to a cross-platform audio API.

This is the project's **first runtime dependency**. Everything until now has been stdlib
plus development tooling, so this is the point at which Article 12 stops being theoretical.

## Decision

Adopt **`sounddevice` 0.5.6** (MIT) as the capture backend, behind a `CaptureBackend` port
so it stays replaceable and testable without hardware.

## Admission review (Article 12)

Evaluated by fetching the wheel with `pip download --no-deps` and inspecting it, before
anything was installed or executed.

| Criterion | Finding |
| --- | --- |
| **Concrete need** | Yes. Cross-platform microphone capture cannot be done from the standard library. |
| **Licence** | MIT (`License-Expression: MIT`), Matthias Geier, 2015–2025. Compatible with this repository's Apache-2.0. |
| **Publisher / provenance** | `spatialaudio/python-sounddevice`, a long-established project by a named maintainer. |
| **Transitive footprint** | One required dependency: `cffi` (which brings `pycparser`). `numpy` is an optional extra we do not enable. Three packages total. |
| **Install / build scripts** | None. It is a wheel; installation unpacks files and runs no project code. |
| **Runtime capabilities** | Opens audio input devices. That is the whole point, and it is the most privileged thing this project does so far. |
| **Known vulnerabilities** | None flagged at time of adoption. Dependabot now watches it, as it does every other pin. |
| **Alternatives** | `PyAudio` — also PortAudio, but a C extension needing a compiler on some platforms, and less actively maintained. `soundcard` — BSD-3, ctypes-based, no native audio callback and thinner platform coverage. Writing our own bindings — rejected outright; audio device handling is exactly the kind of thing handbook 45 says to take from a maintained project. |

### The part that deserves attention: bundled binaries

The Windows and macOS wheels **ship prebuilt PortAudio shared libraries**:

```
_sounddevice_data/portaudio-binaries/libportaudio.dylib
_sounddevice_data/portaudio-binaries/libportaudio64bit.dll
_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll   (and 32-bit, arm64)
```

These are opaque native code that this project does not build and cannot easily audit. That
is a genuine supply-chain exposure and is recorded rather than glossed over.

What mitigates it, honestly stated:

- PortAudio itself is MIT (Ross Bencina, Phil Burk) and is one of the most widely deployed
  audio libraries in existence.
- The binaries are built by GitHub Actions from a published workflow, which the wheel
  actually includes (`portaudio-binaries/.github/workflows/build-libs.yml`). That is
  meaningful provenance, though it is **not** a reproducible-build claim and must not be
  described as one (Article 12, Invariant 14).
- The version is pinned, so the binary cannot change without a visible Dependabot pull
  request.

What does not mitigate it: nothing here is hash-pinned yet. That gap already exists for the
development dependencies and now extends to a native artefact, which raises its priority.

**The ASIO variants are not used.** Those DLLs are built against the Steinberg ASIO SDK,
which carries its own licensing terms. `sounddevice` loads the non-ASIO library unless
`SD_ENABLE_ASIO` is set, and this project never sets it.

### Linux needs a system package

The Linux wheel bundles no binary; `sounddevice` loads the system `libportaudio2`. So on
Linux — including CI — `libportaudio2` must be installed separately. CI installs it, so the
import is exercised on every pull request rather than only on a developer's laptop.

## Consequences

**Gained**

- A real microphone on Windows, macOS and Linux.
- The pipeline becomes observable end to end for the first time, which is the precondition
  for replacing the provisional numbers in `docs/PERFORMANCE_BUDGET.md` with a measured
  baseline.

**Accepted costs**

- Opaque native code in the dependency tree, mitigated only as described above.
- A system package requirement on Linux, which every Linux user must satisfy.
- Three new packages where there were zero.

**Constraints this puts on the code**

- `sounddevice` is imported **only** in `src/on_the_fly/infrastructure/audio/`, never in
  `domain/`. ADR 0002 requires the core to stay ignorant of what it is attached to, and
  phase 2 replaces this binding without touching the pipeline.
- The import is **lazy**, inside the backend, so the domain and its tests run with no audio
  library present at all.
- The device is opened when capture starts and closed deterministically when it ends. A
  microphone held open by an idle application is a privacy problem regardless of whether
  anything reads from it.

### Device names are personal data

A device name is metadata, but it is not innocuous: people name their hardware after
themselves, and "Ilya's AirPods" identifies a person. Device names must not enter logs,
metrics, telemetry or crash reports. They may be shown to the user who owns the device,
because they are the one who named it.

## Review trigger

Revisit if `sounddevice` becomes unmaintained, if a vulnerability is reported against it or
PortAudio, or when hash-pinning is introduced — at which point the native artefact should
be among the first things pinned.
