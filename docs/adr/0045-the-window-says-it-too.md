# ADR 0045 — The window learns nothing from a recogniser producing nothing

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** @tehki
**Risk:** LOW — one more row in a warning area that already exists

## Context

[ADR 0044](0044-speech-went-in-and-nothing-came-out.md) gave the command line something to
say when speech arrives and no text comes out of it. The window — which is where a person
actually sits — said nothing at all, and could not have.

`PipelineWorker` drove every one of its reports from the event loop:

```python
for event in events:
    (self.final if event.is_final else self.partial).emit(event.text)
    report_levels()
```

**A run that produces no events produces no iterations of that loop.** The measured
wrong-language case produces exactly that: no partials, no finals, for the whole run
([ADR 0044](0044-speech-went-in-and-nothing-came-out.md)). So the window sat on *listening*
and did not even update its **input-quality** warnings — a microphone clipping badly during a
run that recognised nothing was a warning the user never saw, for the same reason.

That is a defect independent of the new feature, and it is the interesting half of this
change.

## Decision

**`StreamingRun` reports after every frame, not after every event.**

- An optional `on_frame` callback, invoked once per frame whether or not the frame produced
  anything.
- `speech_seconds` and `finals_so_far` are readable **while the run is in flight**. Waiting
  for `stats` means waiting for the end of a live capture, which is after the user has given
  up.
- The worker moves its level, overflow and silence reporting into that callback. Emitting
  stays change-only: a signal per frame would be a repaint per frame.

**And the window says it, in the row that already exists for this kind of thing:**

```text
speech is arriving and nothing is being recognised — is this English?
```

Third in priority behind the input verdict and the dropped-block count, because both of those
*explain* no text: an input too loud or a run losing blocks is a reason for silence. This row
is for when the audio is fine and the model still has nothing to say.

**Five seconds of speech, where the command line uses half a second.** That figure is
retrospective — reported once a run has finished — while this one interrupts somebody who is
still talking. The measured wrong-language runs produced nothing from 5.3 s and 10.8 s of
speech, and a run that is working shows its first text about 1.1 s in. Five is late enough to
be sure and early enough that nobody has repeated themselves twice.

It clears the moment anything is recognised, and on stop. A warning that stays up after the
thing it warned about has stopped is how a user learns to ignore the row it lives in.

## What this does not do

- **It does not add a poll.** The callback is driven by the audio, at the frame rate the
  source already delivers, and does nothing but read two numbers and compare them.
- **It does not change what is recognised or segmented.** The detector behind
  `speech_seconds` counts; the transducer still does its own endpointing.
- **It does not catch a wrong model that has something to say**, which is ADR 0043's finding
  and unchanged: confident nonsense arrives as finals and looks like success from here.
- ~~**It has not been seen by a person in the window.**~~ **Rendered and asserted, 2026-09-10.**
  `tests/test_ui_window_render.py` builds the real window under Qt's `offscreen` platform and
  pushes states through it, so the label's text — and the priority between the three warning
  rows — is checked against the widget rather than against the model that feeds it. That
  covers every row in that area, none of which had ever been rendered in a test.

  What is still unobserved is a **person** reading it. The wording is the part a test cannot
  judge, and "is this English?" may read as an accusation rather than a hint.

## Review trigger

When the window is next driven by hand — the wording is the part that cannot be tested, and
"is this English?" is a question a user may read as an accusation rather than a hint.
