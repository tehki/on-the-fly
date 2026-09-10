# ADR 0046 — A file at the rate the models take, when the caller asks

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** @tehki
**Risk:** LOW — an opt-in flag and a better message; the default is unchanged

## Context

Two things happen to somebody who points this application at a recording that is not the
16 kHz mono WAV every pinned model takes.

**A recording in another container** gets:

```text
error: not a readable WAV file: file does not start with RIFF id
```

True, and useless. It does not say what the file appears to be, that only WAV is read, or
what to do about it.

**A WAV at another rate** gets refused by the recogniser, correctly and for a good reason:

```text
error: this model expects 16000 Hz audio but was given 44100 Hz.
       Resample deliberately upstream; this recogniser will not do it silently.
```

The awkward part is that this application **already resamples**. Every microphone it opens
goes through libswresample when the device refuses 16 kHz ([ADR 0013](0013-capture-rate-negotiation.md)),
and `WavFileSource` deliberately does not:

> **No resampling.** A file at 44.1 kHz is exposed as 44.1 kHz, not silently converted. Sample
> rate conversion changes the audio a recogniser will see, and doing it invisibly inside a
> file reader is how a model ends up being fed something nobody chose. **The caller decides.**

That refusal is right. What was missing is any way for the caller to decide.

## Decision

**`--resample`, on the three commands that read files, off by default.**

`ResampledSource` wraps a source and yields whole frames at 16 kHz through the same
`Resampler` the microphone path uses. The run says what it did:

```text
resampled     44100 Hz -> 16000 Hz
```

- **Opt-in, and printed.** The file reader still refuses to convert on its own, so nothing
  about the default changes: a file is exposed at its own rate unless somebody says otherwise,
  and when they do the conversion appears in the output rather than in a docstring.
- **A file already at 16 kHz is not wrapped at all** — converting 16 kHz to 16 kHz is a filter
  applied for nothing, and a line of output claiming a conversion that did not happen.
- **Stereo is refused rather than mixed.** Choosing which voice to keep is a decision about
  content, not a conversion.
- **The header's own claims are passed through unrescaled.** `declared_seconds` and
  `truncated_seconds` are facts about the file, and the truncation check compares what a file
  said about itself with what it handed over.

**And an unreadable file is told what it looks like.** Twelve bytes are sniffed against the
containers somebody is most likely to have a recording in, and the message names the format
and the one command that converts it:

```text
error: not a readable WAV file: file does not start with RIFF id. It looks like an MP4
       container — M4A, AAC or a video. Convert it first:
         ffmpeg -i interview.m4a -ac 1 -ar 16000 interview.wav
```

A file of no recognised kind gets the rule rather than a guess. Guessing wrongly is worse
than not guessing.

## What it costs, measured

The published English clip, upsampled to 44.1 kHz with the same library and streamed back
through `--resample`:

```text
[   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE
                   THE SQUALID QUARTER OF THE BROTHELS
confidence    -0.25 median over 1 final(s)
```

Word-perfect against the reference, and the recogniser's own confidence is **−0.25** — the
same figure the native 16 kHz file produces (ADR 0043). A round trip through 44.1 kHz and back
costs nothing measurable on this clip.

## What this does not do

- **It does not decode anything but WAV.** MP3, M4A, Ogg and FLAC are named in the error and
  not read. Decoding them means a media dependency, and `av` being installed for the resampler
  is not an argument for making this application a general audio reader — that is a separate
  decision with its own Article 12 review, and nobody has needed it yet.
- **It does not resample the microphone differently.** That path is unchanged and was already
  doing this.
- **It does not make 44.1 kHz the tested rate.** Every accuracy figure in this project is
  measured on 16 kHz audio. The one clip above says a conversion is not obviously destructive;
  it does not establish a word error rate for resampled input.
- **It does not upsample sensibly.** 8 kHz audio would be accepted and converted, and nothing
  here warns that a model trained on 16 kHz speech is being fed interpolation. That is worth a
  measurement before it is worth a message.

## Review trigger

If anyone measures word error on resampled audio across more than one clip, or when somebody
asks for a container this refuses often enough that the dependency question is worth opening.
