# GitHub repository governance

`REPOSITORY_GOVERNANCE_v1.2-otf1.yaml` describes the controls this repository requires.
This document describes the controls GitHub is actually enforcing, how to apply them, and
how to verify them.

The two are not the same thing, and the difference is the point. A manifest, a CODEOWNERS
file, a pull request template, and a CI workflow are all local files. Someone with push
access can ignore every one of them. Only the provider's branch protection or ruleset
control plane can refuse a write. Constitution Article 11 and Article 2 both forbid
describing this repository as protected on the strength of the files in it.

## Verified remote state

| Checked | Date | Result |
| --- | --- | --- |
| `main` protection | 2026-09-01 | **Active** — ruleset `main-protection`, id `22044161` |
| Enforcement | 2026-09-01 | `active`, `bypass_actors: []`, `current_user_can_bypass: never` |
| Rules present | 2026-09-01 | `deletion`, `non_fast_forward`, `required_linear_history`, `pull_request`, `required_status_checks` |
| Required check | 2026-09-01 | `quality`, with `strict_required_status_checks_policy: true` |
| Required approvals | 2026-09-01 | 0 — see `EXC-2026-09-01-001` |
| Actions workflows | 2026-09-01 | Present; `quality` and `main push provenance` both reporting |
| Dependabot vulnerability alerts | 2026-09-01 | **Enabled** |
| Collaborators with write access | 2026-09-01 | 1 (`tehki`) |
| Repository visibility | 2026-09-01 | Public |

Verified by reading the ruleset back from
`GET /repos/tehki/on-the-fly/rulesets/22044161` after creation, rather than trusting the
response to the write, and confirmed behaviourally by observing a direct push to `main`
be rejected.

This table is a record of a point in time, not a claim about now. Re-run the verification
below after any administrative change and update it.

`EXC-2026-09-01-002` is closed. Its compensating provenance check was kept rather than
removed: it now guards against the ruleset being disabled as much as against a direct
push, which is the failure mode a table like this one cannot detect on its own.

### Prior state, for the record

Before 2026-09-01 this repository had no branch protection, no rulesets, no workflows,
and Dependabot alerts disabled. Kept here because a governance document that quietly
replaces its own history makes it impossible to tell an enforced control from one that
was always assumed.

## Order of operations

Apply protection **after** the `quality` workflow exists on `main`, never before.

A required status check is matched by name against checks that actually report. If
`quality` is required before any workflow on `main` can produce it, every pull request
waits forever on a check that will never arrive, and the only way out is to remove the
protection you just added.

1. Merge the pull request that adds `.github/workflows/ci.yml`.
2. Confirm the `quality` check has reported at least once on `main`.
3. Apply the ruleset below.
4. Verify it by reading it back.
5. Update the table above and close `EXC-2026-09-01-002`.

## Required configuration

Expressed as a repository ruleset. `bypass_actors` is deliberately empty: a rule the
administrator can step around is not enforcement, and the sole maintainer is an
administrator.

`required_approving_review_count` is `0` because GitHub does not permit a pull request
author to approve their own pull request, and this repository has one maintainer. That is
a documented limitation with compensating controls, recorded as `EXC-2026-09-01-001` — not
a decision that review does not matter. It becomes `1` the day a second reviewer joins.

```bash
gh api --method POST repos/tehki/on-the-fly/rulesets --input - <<'JSON'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [{ "context": "quality" }]
      }
    }
  ]
}
JSON
```

What each rule satisfies:

| Rule | Manifest field |
| --- | --- |
| `deletion` | `branch_deletion_allowed: false` |
| `non_fast_forward` | `force_push_allowed: false` |
| `required_linear_history` | `require_linear_history: true` |
| `pull_request` | `pull_request_required: true`, `direct_push_allowed: false`, `dismiss_stale_approvals`, `require_conversation_resolution` |
| `required_status_checks` with `strict` | `required_status_checks: [quality]`, `require_up_to_date_before_merge: true` |
| `bypass_actors: []` | `enforce_for_administrators: true` |

Also enable Dependabot alerts, which Article 12 expects and which are currently off:

```bash
gh api --method PUT repos/tehki/on-the-fly/vulnerability-alerts
```

## Verification

Applying a setting and confirming a setting are separate acts. Read the state back from
the API rather than trusting the response to the write.

```bash
gh api repos/tehki/on-the-fly/rulesets --jq '.[] | {id, name, target, enforcement}'
```

```bash
gh api repos/tehki/on-the-fly/rulesets/RULESET_ID --jq '{enforcement, bypass_actors, rules: [.rules[].type]}'
```

The verification passes only when `enforcement` is `active`, `bypass_actors` is empty, and
the rule list contains `deletion`, `non_fast_forward`, `required_linear_history`,
`pull_request`, and `required_status_checks`.

A useful negative check, since a control that has never been observed refusing anything is
an assumption: from a clean clone, confirm that a direct push to `main` is rejected.

## Capability note

The `gh` CLI on the maintainer's machine is authenticated as `tehki` with `repo` scope and
`ADMIN` permission on this repository, so it *can* write these rules. Under Constitution
Article 3 that is a capability, not an authorisation: `ADMIN` is a distinct permission from
repository write, and an agent must hold an explicit scoped grant before using it. The
manifest records the capability as available and the grant as separately required.

## The provenance check, kept

`scripts/verify_main_push_provenance.py` runs on every push to `main` and fails when the
push was forced, was not a fast-forward, or carries a commit GitHub did not sign — the
signature being what distinguishes a merge performed through a pull request from a commit
pushed straight to the branch.

It was written as a compensating control for the window before the ruleset existed. It is
kept now that the ruleset is active, because the two controls fail differently: the
ruleset prevents an unauthorised push while it is configured, and the provenance check
notices if it stops being configured. A ruleset can be deleted by an administrator in a
few seconds, and nothing else in this repository would report that.

It remains detection, not prevention, and is not a substitute for the ruleset.
