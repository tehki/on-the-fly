# ADR 0036 — Admitting the package that fetches every model

**Status:** Accepted
**Date:** 2026-09-07
**Deciders:** @tehki
**Risk:** MODERATE — a dependency admission on the path that reaches the network for model weights

## Context

`requirements.txt` opens by saying:

> Every entry here passed the Article 12 admission review recorded in `docs/adr/`.
> Versions are pinned so a build is reproducible in the weak sense (same declared inputs).

Three entries in that file were promoted from transitive to declared for one stated reason,
written out each time: a package that arrives under another package is *that package's*
choice, and this project now imports it directly. `ctranslate2` arrived under
`faster-whisper`; `onnxruntime` under `sherpa-onnx`; `av` under `faster-whisper` again.

Nothing checked the converse. `huggingface_hub` is imported directly, in
`src/on_the_fly/infrastructure/model_store.py`, and was declared in no requirements file at
all. It was present only because `faster-whisper` asks for `huggingface-hub>=0.21`.

That is the same situation as the other three, on a worse file. `model_store.py` is the one
place in this project that reaches the network for model weights, and it is named explicitly
in `security_sensitive_paths` for that reason. Three consequences followed:

- **It was unpinned.** Every other runtime dependency is `==`-pinned. This one resolved to
  whatever `>=0.21` allowed on the day, so the file header's reproducibility claim was false
  for precisely the package that downloads the models.
- **`faster-whisper` could drop it.** This is the risk the file cites in ADR 0009's entry —
  "removes the risk of faster-whisper dropping it" — applied to a package it had not applied
  it to.
- **The error message was wrong.** `_download` raises "huggingface_hub is required to
  download a model; install the runtime requirements", and installing the runtime
  requirements would not have installed it.

It was found by taking `requirements.txt`'s own stated policy and applying it uniformly to
every import under `src/`, which nothing had done.

## Decision

**Declare and pin `huggingface_hub==1.30.0`**, with the admission recorded here, and
**make the file's claims executable** so the next omission fails the gate rather than
waiting to be noticed.

### The admission

| | |
|---|---|
| Licence | Apache-2.0 |
| Version | 1.30.0, already installed |
| Added bytes | none — it is in the tree today, under `faster-whisper` |
| Imported | `infrastructure/model_store.py` only, lazily, inside `_download` |
| Reached | only when `allow_download` is true, which is opt-in and off by default |

Nine transitive packages, none new: `click` (BSD-3-Clause), `filelock` (MIT), `fsspec`
(BSD-3-Clause), `hf-xet` (Apache-2.0), `httpx` (BSD-3-Clause), `packaging` (Apache-2.0 OR
BSD-2-Clause), `pyyaml` (MIT), `tqdm` (MPL-2.0 AND MIT), `typing-extensions` (PSF-2.0). All
permissive. `tqdm`'s MPL-2.0 half is weak copyleft at file scope and is discharged by using
it unmodified as a library, in the same way ADR 0016 discharges PySide6's LGPL.

`hf-xet` deserves naming rather than listing. It is the Xet transfer backend, and during the
French model work a `snapshot_download` through it sat at zero bytes for twenty-five minutes
before it was abandoned in favour of `curl` with a content-length check. That is not an
argument against admitting the package — the pinned path still works and the fallback exists
— but it is a known property of this dependency and belongs in the record.

**What this pin does not do.** It does not make the downloaded weights trustworthy. That is
`ModelPin` and `ModelStore.verify`: every file is checked against a SHA-256 recorded in the
source tree, a mismatch is refused and the file left in place for inspection, and an unpinned
model cannot be loaded at all. Those controls assume nothing about the fetcher. The pin here
is about admitting the fetcher deliberately, at a known version, rather than inheriting it.

### The three checks

Added to `scripts/validate_repository_governance.py`, which already checks that the
governance document is consistent with this tree:

- **`check_declared_dependencies`** — every non-stdlib, non-first-party import root under
  `src/` matches a distribution some requirements file declares. Names are folded the way
  pip compares them, which covers `faster_whisper`, `sherpa_onnx` and `huggingface_hub`
  without a table; only `PySide6-Essentials`, whose package name differs from its
  distribution name, needs an entry. This check fails on `main` as it stands and passes with
  the line above.
- **`check_third_party_imports_are_lazy`** — no third-party package is imported at module
  level. Every entry in `requirements.txt` claims this of its package, and the claim is what
  lets the domain and its tests run with none of the heavy engines installed, keeps `--help`
  from loading ONNX Runtime, and keeps the GUI toolkit genuinely optional. One module-level
  import in one file ends all three for the whole package.
- **`check_the_domain_imports_nothing_third_party`** — `domain/` imports no package at all.
  `requirements.txt` says "never in `domain/`" of each engine, and it is what let a second
  translation engine land in ADR 0018 without anything above `infrastructure/` noticing. It
  is never broken deliberately and is broken easily: one convenience import of numpy in a
  domain module and the pure layer is not.

The second and third pass on the tree unchanged. They are here because they were true by
habit and by nothing else, and a rule that survives on habit has already been broken once
in this file — which is what this ADR is about.

## Consequences

The `_download` error message becomes true without being edited: the runtime requirements
now do contain what it names.

A future contributor who imports a new package gets a named failure from `make check` with
the Article 12 sentence attached, rather than a working tree and an admission that never
happened. The cost is one line in `requirements.txt` and a decision, which is the right
amount of friction for adding executable trust to a project arranged around a retention
promise.

Pinning `huggingface_hub` independently of `faster-whisper` means the two can now disagree.
That is the point — the disagreement becomes a resolver error at install time instead of a
silent version drift — but it does mean this pin has to move when `faster-whisper` does.

## Alternatives considered

**Leave it transitive and stop importing it directly.** The download would have to go
through `faster-whisper`'s API, which does not expose `snapshot_download` for arbitrary
pinned revisions. It would trade a declared dependency for a worse one.

**Pin a range rather than an exact version.** Every other entry is exact. A range here would
reintroduce the drift this ADR is about, in the file that says it does not have any.

**Write the checks as tests rather than governance checks.** Dependency admission is
Article 12, a policy matter, and `scripts/validate_repository_governance.py` is where
tree-consistency rules with a policy behind them already live — alongside
`check_source_classification` and `check_referenced_paths_exist`, both written after the
same kind of omission. It also runs before the suite, so the failure arrives first.
