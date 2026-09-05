# ADR 0016 — A desktop window with no scrollback

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — first user-facing surface, and a new dependency with licence obligations

## Context

ADR 0002 put a PySide6 desktop interface in phase 1 and nothing had been built. Everything
until now has been a command line, which is a fine way to develop a pipeline and no way to
hold a conversation.

## The decision that shaped the interface

`session_caption_scrollback` is `EPHEMERAL` with `enabled_by_default: false` and
`default_retention_seconds: 0`, and `docs/RETENTION_POLICY.md` states the consequence in one
line: **live translation is fine, scrollback is not.**

So the window shows **one utterance**. When the next is finalised the previous is gone —
not scrolled, not collapsed, gone. Stopping clears it. Changing language clears it. A
failure clears it.

That is the single most consequential decision here, and it is worth being honest about the
cost rather than presenting it as a feature: **a user who looks away misses what was said.**
For a live translator that is a real loss. The alternative is an application that accumulates
a transcript of a private conversation on screen and in memory, which is precisely what
ADR 0001 and the ten-second rule exist to prevent. A history pane is the easiest feature in
this application to build — appending instead of replacing is a one-character difference —
and it would quietly break the promise the whole project is arranged around.

Adding one later is possible. It requires a record in `docs/EXCEPTIONS.md` with an owner and
an expiry, which is the correct amount of friction for a decision that changes what the
product retains.

## Structure

```text
caption.py   ViewState, CaptionModel   no Qt, fully tested, holds every decision
window.py    widgets and a stylesheet  turns a ViewState into pixels, nothing else
app.py       composition root          wires a worker thread to the model
```

The interesting logic is in the file with no Qt in it. That is not a purity exercise: it is
what lets 18 tests assert on *what the window shows* without a display, in CI that has none.

**The pipeline runs on a worker thread.** Recognition and translation both block, and a
blocked Qt event loop is a frozen window — the most common way an application of this shape
is bad. The worker emits signals; Qt marshals them; the window renders.

**Audio never reaches the UI layer.** The worker consumes frames inside the pipeline and
emits text. Nothing in `ui/` holds a buffer, and the retention store purges on exit exactly
as it does for the command line.

## Admission review — PySide6 (Article 12)

| Criterion | Finding |
| --- | --- |
| Licence | **LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only** |
| Need | Concrete: the interface ADR 0002 planned |
| Footprint | 76 MB (`PySide6-Essentials`), one transitive package (`shiboken6`) |
| Install scripts | None. Wheel with bundled Qt libraries |
| Alternatives | Toga (BSD, native widgets, weaker styling); Kivy (MIT, non-native look); Tkinter (stdlib, hard to make pretty) |

**The LGPL obligation is real and is discharged by how Python uses it.** The library is
imported dynamically from a wheel the user installs and can replace with `pip install`, this
project makes no modifications to it, and it is an optional extra rather than something
bundled. The notice belongs in any distributed build, and that is recorded here rather than
assumed.

**It is an optional extra**, in `requirements-ui.txt` rather than `requirements.txt`. The
pipeline, the command line and all 319 core tests run without a GUI toolkit installed, and
CI installs it nowhere — a 76 MB wheel on every run, to test code that has no display, would
be paying for nothing.

## Why PySide6 and not a toolkit that also does mobile

Toga and Kivy target phones as well as desktops, and reusing one interface across both is
genuinely appealing. It was not taken, for a reason that survives scrutiny better than
toolkit preference:

**The mobile port does not reuse the desktop UI regardless.** Phone audio capture is not
PortAudio, the permission model is different, the interaction is different — a phone
translator is a full-screen caption a user props up on a table, not a window. What has to be
portable is the **pipeline**, and that is already behind ports precisely so phase 2 replaces
edges rather than the core (ADR 0002). Choosing a weaker desktop toolkit to share widgets
that would be rewritten anyway trades something real for something notional.

Today's work makes that more true, not less: both models now run single-threaded (ADR 0014),
which is what a phone's cores want.

## Consequences

- The project has a user-facing surface for the first time, reachable as
  `python -m on_the_fly gui`.
- Partials are shown dimmed and finals bright, so "this may still change" is visible without
  a word for it. Translations appear only under finals (ADR 0009).
- **Dropped audio is shown to the user**, not hidden: "N audio block(s) dropped — some speech
  was lost". A translator that silently omits a sentence is worse than one that admits it.
- The CC-BY-4.0 attribution required by ADR 0009 now has the user-reachable home it was
  promised, in the window footer.
- The language pickers default to English→Russian — the pair that is pinned and measured —
  rather than whatever sorts first alphabetically.

## What has not been done

- **The window has never captured live speech**, because the reference machine's microphone
  produces saturated audio (ADR 0015; diagnosed in ADR 0019 as capture gain pinned at
  +30 dB, and now reported to the user rather than transcribed). It has been rendered and
  driven through every state
  with synthetic ones. The worker's pipeline path is the same one the command line uses and
  is covered by that path's tests, but *the two together* are unverified.
- No device picker. The default input device is used, and choosing between devices is a real
  feature this does not have.
- No packaging. Running it needs a checkout and `pip install -r requirements-ui.txt`.

## Review trigger

Before phase 2, and before any scrollback, history, or export feature — each of those needs
an entry in `docs/EXCEPTIONS.md` before it needs code.
