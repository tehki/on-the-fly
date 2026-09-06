# ADR 0029 — The reference room, measured

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — a measurement and a script; nothing in the pipeline changes

## Context

[ADR 0027](0027-reverberation.md) showed that reverberation drives live accuracy, using
synthetic shoebox rooms, and listed as a limitation that none of it was measured anywhere
real. [ADR 0028](0028-blind-reverberation-estimate.md) then showed what happens when a
conclusion drawn from synthetic audio meets the world: a statistic that scored 88% on
thirty-four synthetic points inverted on five real ones.

So the room itself was measured.

## Method

`scripts/measure_room.py`. Four six-second exponential sine sweeps through the speakers,
recorded on the microphone and deconvolved with the inverse filter (Farina). Averaging
coherent sweeps lifted the measurement from **13.1 dB to 28.5 dB** of signal to noise — the
sweep adds in phase each time and the room's noise does not.

The mixer was returned to its recorded state afterwards and the restoration verified by diff.

### The first estimate was wrong, and it is worth saying how

Fitting the whole 1.5-second Schroeder curve gave **RT60 = 1.32 s** for a response with 99.3%
of its energy inside the first 100 ms. Those cannot both be true. The curve stops decaying
once it reaches the measurement noise floor at about 200 ms, and a fit that runs past that is
fitting noise. Restricted to the genuinely decaying region the answer changes completely, and
is still soft:

| fit window | RT60 |
| --- | --- |
| **20–120 ms** | **0.62 s** |
| 20–150 ms | 0.77 s |
| 30–150 ms | 0.85 s |
| 20–200 ms | 1.08 s |
| 40–180 ms | 1.17 s |

The spread is the honest error bar: **the room is live, RT60 on the order of 0.6–0.9 s**, and
this method on this hardware cannot say it more precisely than that.

## What the measurement cannot tell us

The speaker and the microphone are a hand's width apart inside the same laptop. Measured,
**91.5% of the energy arrives within 5 ms of the peak** and 99.3% within 100 ms, giving a
direct-to-reverberant ratio of about +5.7 dB. That is nothing like a person speaking from a
metre away, and the point is not subtle: running the clean speech sample through this measured
path gives **2.1% word error**.

So this path is fine. RT60 transfers, because it is a property of the room rather than of
where the two ends sit in it; DRR does not, and is reported here only to show how unlike a
talking person the geometry is.

## What it does tell us, and it closes the loop

RT60 around 0.6–0.9 s places this room in the livelier half of ADR 0027's grid. What that grid
measured there:

| RT60 | distance | WER |
| --- | --- | --- |
| 0.67 | 0.4 m | 2.1% |
| 0.69 | 0.7 m | 6.2% |
| 0.67 | 1.0 m | **35.4%** |
| 0.67 | 1.5 m | 33.3% |

A person at a laptop sits somewhere between 0.4 m and 1.0 m, which is exactly where that
column falls off a cliff. The live runs in this session ranged from substantially correct to
roughly half the words wrong, from the same speaker in this room, and that is the predicted
behaviour rather than a mystery.

## Decision

**Record the room, keep the instrument, change nothing in the pipeline.**

`scripts/measure_room.py` goes in beside `scripts/measure_pauses.py`, for the same reason: a
number that a decision rests on should be re-derivable rather than trusted. ADR 0028's lesson
argues for keeping tools that measure the real world specifically.

No product change follows. ADR 0027's advice — sit closer, or use a headset — is now
quantified for this room rather than hypothetical, and ADR 0028 established that there is no
way to time it automatically.

## What this does not do

- **It is one position and one measurement.** RT60 varies across a room and this is a single
  speaker-to-microphone path in it.
- **It does not separate the room from the hardware.** The response includes the speaker, the
  microphone, and whatever structural coupling exists between them through the chassis.
- **It gives no per-band answer.** An octave-band decomposition returned mostly `nan`, because
  each band hits the noise floor before completing a usable decay. A real answer needs a
  louder source or a quieter room.
- **It does not measure where a speaker's mouth is**, which is the geometry that matters, and
  cannot with the built-in speaker.

## Consequences

- The reference machine's room is documented as live, which is context for every live
  measurement this project has taken and for every one it takes next.
- The chain that began with "why is live recognition sometimes bad" is closed: not the gain
  (ADR 0020), not the level (ADR 0026), the room (ADR 0027) — and the room is now measured
  rather than hypothesised.

## Review trigger

When an external speaker can be placed where a person's head would be, which is the
measurement this one is standing in for; or when a second room can be measured, since every
acoustic conclusion in this project rests on this one.
