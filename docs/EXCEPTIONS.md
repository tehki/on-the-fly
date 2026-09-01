# Exception register

Every security, retention, isolation, capability, or repository-governance exception lives
here. Constitution Article 13 requires each one to carry an owner, reason, scope, risk,
approving authority, compensating controls, issue time, explicit expiry, and removal
condition. An exception that has passed its expiry authorises nothing, whatever the code
still does.

`scripts/validate_repository_governance.py` fails the build if the governance manifest
cites a record that is not present in this file.

## Status legend

| Status | Meaning |
| --- | --- |
| ACTIVE | Within its expiry window and still needed |
| EXPIRED | Past expiry. Authorises nothing. Either renew deliberately or remove the behaviour |
| REMOVED | Removal condition met and the exception withdrawn |

---

## EXC-2026-09-01-001 — Human approval cannot be enforced on `main`

| Field | Value |
| --- | --- |
| **Status** | ACTIVE |
| **Owner** | @tehki |
| **Reason** | `tehki/on-the-fly` has one maintainer with write access. GitHub does not allow the author of a pull request to approve it. A non-zero `required_approvals` combined with `enforce_for_administrators` would block every merge, so the review requirement in Constitution Article 11 cannot be technically enforced at the current reviewer population. |
| **Scope** | The `required_approvals` and `require_code_owner_review` settings on `main` only. No other governance control is relaxed. |
| **Risk** | MODERATE. No second human reviews a change before it reaches `main`. A mistaken or malicious change by the sole maintainer, or by anything acting with their credentials, is not caught by peer review. |
| **Approved by** | @tehki, repository owner, 2026-09-01 |
| **Issued at** | 2026-09-01 |
| **Expires at** | 2027-03-01 |
| **Removal condition** | A second reviewer with write access is added to the repository. On that day, set `required_approvals` to 1 (2 for CRITICAL), enable `require_code_owner_review`, apply the change to the remote ruleset, verify it, and mark this record REMOVED. |

**Compensating controls**, all of which must remain in force for this exception to hold:

1. A pull request is still required for every change to `main`; direct push is disabled.
2. The `quality` status check must pass before merge, and it is not permitted to be
   renamed, skipped, or weakened.
3. `enforce_for_administrators` applies the rules to the maintainer too, so the
   requirement cannot be quietly stepped around.
4. Force push and branch deletion are disabled.
5. Linear history and conversation resolution are required.
6. `scripts/verify_main_push_provenance.py` reports any push to `main` that did not
   arrive as a GitHub-signed merge.
7. The pull request template requires a written risk, capability, retention, and
   verification declaration, which stands in for some of what a reviewer would ask.

**Review trigger:** revisit at expiry, or immediately if a second maintainer joins, or if
a change classified CRITICAL is proposed.

---

## EXC-2026-09-01-002 — Remote branch protection is not yet configured

| Field | Value |
| --- | --- |
| **Status** | ACTIVE |
| **Owner** | @tehki |
| **Reason** | Verified on 2026-09-01: `main` has no branch protection and the repository has no rulesets. The governance manifest requires both. Protection is deliberately applied *after* the `quality` workflow exists on `main`, because a required status check that no workflow can report would block every pull request permanently. |
| **Scope** | The window between adopting this governance stack and applying the remote ruleset. Nothing else. |
| **Risk** | HIGH while open. Nothing at the provider prevents a direct push, a force push, or a deletion of `main`. Local manifests are not enforcement (Constitution Article 11). |
| **Approved by** | @tehki, repository owner, 2026-09-01 |
| **Issued at** | 2026-09-01 |
| **Expires at** | 2026-09-08 |
| **Removal condition** | The ruleset described in `docs/GITHUB_REPOSITORY_GOVERNANCE.md` is applied and independently verified by reading it back from the API. Record the verification output there, then mark this record REMOVED. |

**Compensating controls:**

1. `scripts/verify_main_push_provenance.py` runs on every push to `main` and fails on a
   forced push, a non-fast-forward, or a commit GitHub did not sign.
2. The repository has a single maintainer, so the population able to push is one account.
3. This window is intentionally short and has a one-week expiry rather than an open end.

**Review trigger:** at expiry, or as soon as the ruleset is applied — whichever is first.

---

## Retention exceptions

None. Every data path currently classified in `CODING_AGENT_POLICY_v1.2.yaml` uses its
default class.

Note for future work: any feature that lets a user scroll back through captions, replay a
past translation, review a session transcript, or export a conversation retains content
past the ten-second `EPHEMERAL` window and therefore requires a record in this section
before it is built. See `docs/RETENTION_POLICY.md`.
