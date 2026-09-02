# ADR 0004 — Project policy documents carry an `-otf` revision suffix

**Status:** Accepted
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — a versioning mistake here makes every other control ambiguous

## Context

The upstream policy stack arrives as four documents written for a different project. Each
adoption re-scopes them: binding Article 11 to this repository, correcting paths to files
that exist here, replacing the `ai_automation_department` namespace, recording the verified
remote state, and adding this project's own retention profiles.

At the first adoption those derivatives were given ordinary version bumps — upstream v1.1
became this project's v1.2. That worked exactly once.

On 2026-09-02 upstream released its own v1.2. The result:

| Document | This project had | Upstream shipped | Same version, same filename |
| --- | --- | --- | --- |
| Constitution | v1.2 (our re-scope of upstream 1.1) | v1.2 (new Articles 15–17) | **collision** |
| Policy | v1.2 | v1.2 | **collision** |
| Repository governance | v1.1 | v1.1 | **collision** |

Not a naming inconvenience. `Article 15` meant *Definition of Done* in our v1.2 and
*Engineering Throughput* in upstream's v1.2, so a cross-reference to "Article 15" could not
be resolved without knowing which v1.2 was meant. Handbook section 0A classifies exactly
this as a defect, and it would have recurred at every future upstream release.

## Decision

Project derivatives carry an explicit revision suffix:

```text
<DOCUMENT>_v<upstream-version>-otf<n>.<ext>
```

`<upstream-version>` names the upstream document this is derived from; `<n>` is this
project's revision against that base. So `CODING_AGENT_POLICY_v1.2-otf1.yaml` is the first
on-the-fly revision of upstream policy 1.2.

Each derivative also records the derivation in its own content — the policy carries a
`derived_from_upstream` block, and the governance manifest a `derived_from_upstream` field
— so the relationship survives being read without its filename.

Collision becomes impossible: upstream v1.3 produces `v1.3-otf1`, which cannot be confused
with anything upstream ships, and the base version is legible at a glance.

## Alternatives considered

**Keep bumping our own numbers** (upstream 1.2 → our 1.3). Rejected: it collides again the
moment upstream reaches 1.3, and it hides which upstream base a document came from.

**Adopt upstream verbatim and put project scoping in a separate overlay document.**
Genuinely attractive — upstream updates become a drop-in replacement. Rejected for now
because the governance manifest cannot work this way: upstream's own manifest declares
`repository_specific_profile: true` and requires path and control review before it is
copied to another repository, and its path list names files that do not exist here. A
validator that requires protected paths to exist would fail against it. Splitting two
documents into overlays while two others stay merged would be harder to follow than one
consistent rule. Worth revisiting if the derivative diffs shrink.

**Take upstream unchanged and accept the mismatch.** Rejected outright: it would reinstate
the defect where Article 11's protection mandate names another repository and therefore, read
literally, does not bind this one.

## Consequences

- Filenames are longer and appear in CODEOWNERS, the manifest's protected paths, both
  validators, and several documents. All are checked by
  `validate_repository_governance.py`, which fails the build if a protected path does not
  exist — so a missed rename cannot pass silently.
- Every adoption is a rename. That is deliberate: it forces the diff to be reviewed rather
  than an existing file being quietly overwritten with different content, which is how the
  collision would have gone unnoticed.
- The upstream base is always visible, so "which upstream version are we on" has an answer
  that does not require reading the file.

## Adoption procedure

1. Copy the upstream document to its `-otf1` name.
2. Re-apply the project deltas — Article 11 binding, project name, namespace, manifest
   pointer, verified remote state, project retention profiles.
3. Grep for references to the previous names and update them.
4. Run both validators. A stale reference fails as a missing protected path.
5. Extend the validators to cover whatever the new upstream version added, with a failing
   test for each new check.
6. Delete the superseded derivatives in the same pull request, so two versions never
   coexist.

## Review trigger

If upstream ever adopts a naming scheme of its own, or if the project deltas shrink enough
that a verbatim-plus-overlay arrangement becomes practical.
