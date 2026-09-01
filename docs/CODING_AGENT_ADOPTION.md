# Coding agent policy stack — adoption record

Adopted 2026-09-01.

## The stack

| Layer | Document | Version |
| --- | --- | --- |
| 2 | `CODING_AGENT_CONSTITUTION_v1.2.md` | 1.2 |
| 3 | `CODING_AGENT_POLICY_v1.2.yaml` | 1.2 |
| 4 | `REPOSITORY_GOVERNANCE_v1.1.yaml` | 1.1 |
| 5 | `CODING_AGENT_DEVELOPMENT_PRINCIPLES_SYSTEM_PROMPT_v1.4.1.md` | 1.4.1 |

Layer 1 is applicable law and contractual obligation. Layer 6 is project convention. A
lower layer may be stricter; it may never silently weaken a higher one.

## What was inherited and what was wrong with it

The four documents arrived written for a different project,
`ai-automation-department`. Adopting them unchanged would have produced governance that
looked thorough and enforced very little. The specific defects, and what was done:

| Defect | Effect if left | Resolution |
| --- | --- | --- |
| Constitution Article 11 named `ai-automation-department` as the repository whose `main` must be protected | Read literally, the branch-protection mandate did not apply to this repository at all | Article 11 rewritten to bind to whichever governance manifest the active policy declares. Constitution 1.1 → 1.2 |
| Governance manifest named and scoped to another project | Same class of problem, plus a project-boundary question under Article 7 | Manifest renamed `on-the-fly-repository-governance`, repository stated explicitly. 1.0 → 1.1 |
| Protected path list named `..._v1.3.md`; the handbook present was v1.4 | CODEOWNERS generated from that list would have guarded a file that did not exist while leaving the real handbook unowned | Paths corrected against files that exist, and `validate_repository_governance.py` now fails the build if any protected path is missing |
| Protected path list included a Telegram transport document | A protected path for a component this project does not have | Removed. The general transport-truthfulness invariant is retained in `docs/SECURITY_PRIVACY.md` |
| Handbook self-identified as v1.3 in section 0A and sections 61–62 | Version drift, which handbook 0A itself classifies as a defect | Corrected. 1.4 → 1.4.1 |
| `ai_automation_department.domain.retention` namespace | Pointed at code in another project | Now `on_the_fly.domain.retention` |
| `current_connector_can_write_branch_rules: false` | Stated a limitation that was not true here, which is its own truthfulness problem | Corrected to `true` after verification, with an explicit note that capability is not authorisation |
| `CRITICAL` required two approvals | Unsatisfiable with one maintainer; every CRITICAL change would have been unmergeable, and the rule would eventually have been quietly deleted | Recorded as an explicit limitation with compensating controls under Article 9, as `EXC-2026-09-01-001` |

## What was added

| Path | Purpose |
| --- | --- |
| `.github/CODEOWNERS` | Ownership routing for security-sensitive paths |
| `.github/pull_request_template.md` | Risk, capability, retention, security, destructive-action and performance declarations |
| `.github/workflows/ci.yml` | The `quality` job — the required status check — plus main-push provenance |
| `.github/dependabot.yml` | Makes pinned-version staleness visible as pull requests |
| `scripts/validate_coding_agent_policy.py` | Fails the build if the policy drops below a Constitution floor |
| `scripts/validate_repository_governance.py` | Fails the build if the manifest stops being true of this tree |
| `scripts/verify_main_push_provenance.py` | Detects pushes to `main` that did not arrive via a reviewed pull request |
| `tests/test_governance_validators.py` | Positive and negative tests — a validator that cannot fail is not a gate |
| `Makefile` | Local mirror of the CI gates |
| `docs/SECURITY_PRIVACY.md` | Security posture for a live translator |
| `docs/RETENTION_POLICY.md` | Retention classes mapped onto this application's data |
| `docs/GITHUB_REPOSITORY_GOVERNANCE.md` | Required remote controls, how to apply them, how to verify them |
| `docs/EXCEPTIONS.md` | The exception register |
| `src/on_the_fly/domain/retention/` | Reserved for retention enforcement; nothing implemented |

## What is not enforced

Stated plainly, because the failure mode of governance work is believing it is finished:

- **No runtime retention enforcement exists.** The policy describes ten-second expiry;
  no code implements it, because no application code exists yet. This is the largest gap
  between what the stack says and what the repository does.
- **Human review is not enforced** and cannot be at the current maintainer count
  (`EXC-2026-09-01-001`). Every other branch control is enforced; this one is not, and
  the exception says so rather than the manifest pretending otherwise.
- **Dependencies are version-pinned but not hash-pinned.**

Remote branch protection *is* now configured and verified — ruleset `22044161`, applied
2026-09-01, read back from the API and observed rejecting a direct push. `EXC-2026-09-01-002`
is closed. See `docs/GITHUB_REPOSITORY_GOVERNANCE.md` for the evidence.

## Working under this stack

Before a change: resolve the boundary and exact target, classify risk, name the minimum
capabilities, and identify what retention class any new data path falls into.

After a change: run `make check` (or `make PYTHON=py check` on Windows), fill in the pull
request template honestly, and state what you actually ran rather than what you expect
would pass.

The rule that catches the most mistakes is handbook 52: do not claim a file, test, control,
branch rule, or CI result exists or passed unless it has been inspected or executed.
