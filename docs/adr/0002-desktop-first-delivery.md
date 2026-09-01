# ADR 0002 — Desktop first, mobile as phase 2

**Status:** Accepted
**Date:** 2026-09-01
**Deciders:** @tehki
**Risk:** MODERATE — determines the framework and the shape of the core

## Context

The product is cross-platform. The question was whether mobile is in scope for the first
release or follows it.

Building for desktop and mobile simultaneously means the inference layer has to be native
from day one — Python does not ship to iOS or Android in any pleasant way — so the ML
pipeline would be written in C++ or Rust behind a Flutter or Tauri shell before we know
whether the pipeline itself works. That is a large bet placed before the first measurement.

## Decision

**Phase 1: desktop.** Windows, macOS, Linux. Python, with the on-device pipeline from
ADR 0001 and a PySide6 (LGPL) interface.

**Phase 2: mobile.** iOS and Android, with the inference layer moved to native
`whisper.cpp` and CTranslate2 behind a cross-platform shell.

Phase 1 exists to answer the questions that decide phase 2: can a small local model keep up
with live speech on ordinary hardware, and does the ten-second retention rule survive
contact with a real audio pipeline.

## The constraint this puts on phase 1 code

Phase 2 is a port of the *edges*, not a rewrite of the *core*. That only holds if phase 1
is written so the core does not know what it is attached to. Handbook section 2 already
requires this; here it has a concrete payoff.

```text
Audio capture adapter  ·  UI adapter  ·  Model runtime adapter
                    ↓
            Application boundary
                    ↓
   Pipeline · Retention · Session policy   ← ports only, no Qt, no torch,
                    ↓                        no platform audio, no filesystem
        Ports / interfaces
                    ↓
   Infrastructure implementations
```

Concretely, in phase 1:

- No Qt import outside the presentation layer.
- No direct model-library call outside a recogniser or translator adapter behind a port.
- Retention lives in `src/on_the_fly/domain/retention/` and depends on an injected clock
  and scheduler, never on a UI event loop.
- Audio capture sits behind a port, so a phone microphone is a new implementation rather
  than a change to the pipeline.

If phase 2 turns out to require rewriting the pipeline, phase 1 leaked its framework into
its core and that is the defect to fix.

## Alternatives considered

**Flutter or Tauri v2 from day one.** Genuinely cross-platform including mobile, and avoids
a later port. Rejected for now: it forces native inference before the pipeline is proven,
and it puts the retention module — the part of this project with the strictest correctness
requirement — in the least convenient language to test first. Handbook 44 applies: take on
that complexity when it is justified, not in advance.

**Web application.** Rejected. Browser speech recognition routes audio to a cloud service,
which contradicts ADR 0001, and local model execution in a browser is constrained.

**Mobile first.** Rejected as the harder environment in which to establish a baseline.

## Consequences

- A working translator sooner, and a real measured baseline before the expensive decision.
- Phase 2 carries a real port cost for the inference layer. Accepted deliberately, and
  reduced by the layering constraint above.
- PySide6 is LGPL. Dynamic linking keeps this project's Apache-2.0 licence intact;
  static linking or bundling would need review.
- Desktop hardware is more forgiving than a phone. Phase 1 performance numbers do not
  transfer to phase 2 and must be re-baselined there — comparing them would be exactly the
  incomparable-measurement error handbook 0L.6 warns about.

## Review trigger

At the end of phase 1, against measured results rather than expectation. Earlier if desktop
latency shows the approach cannot work on phone-class hardware at all.
