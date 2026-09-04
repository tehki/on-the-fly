# Coding agent policy stack — adoption record

First adopted 2026-09-01. Upstream v1.5 stack adopted 2026-09-02.

## The stack

| Layer | Document | This project | Upstream base |
| --- | --- | --- | --- |
| 2 | `CODING_AGENT_CONSTITUTION_v1.2-otf1.md` | 1.2-otf1 | 1.2 |
| 3 | `CODING_AGENT_POLICY_v1.2-otf1.yaml` | 1.2-otf1 | 1.2 |
| 4 | `REPOSITORY_GOVERNANCE_v1.1-otf1.yaml` | 1.1-otf1 | 1.1 |
| 5 | `CODING_AGENT_DEVELOPMENT_PRINCIPLES_SYSTEM_PROMPT_v1.5-otf1.md` | 1.5-otf1 | 1.5 |

Layer 1 is applicable law and contractual obligation. Layer 6 is project convention. A
lower layer may be stricter; it may never silently weaken a higher one.

The `-otf<n>` suffix is not decoration. See [ADR 0004](adr/0004-policy-stack-versioning.md):
upstream released its own v1.2 of the Constitution and policy while this project already had
documents at v1.2, and `Article 15` meant something different in each. The suffix names the
upstream base explicitly so that collision cannot recur.

## The v1.5 adoption (2026-09-02)

Upstream added two Articles and a set of CI concepts. What changed here:

| Upstream addition | Effect on this project |
| --- | --- |
| **Article 15** — engineering throughput without control dilution | Coherent work-unit pull requests are explicitly allowed. The effective risk of a batch is its highest included risk, and unrelated objectives, cross-project changes, and independent privileged boundaries still need their own pull request. Recorded in the manifest under `development_flow`. |
| **Article 16** — cryptographic protection and key separation | Nothing here encrypts anything yet, so this is a floor rather than a description. `validate_coding_agent_policy.py` now enforces the crypto floors so they are in place before the first thing that needs them — a model cache, a spilled buffer, a stored API key. |
| **Article 17** — definition of done | Renumbered from 15; now also requires cryptographic controls to pass. |
| **Validation lanes** (FAST / FULL / RELEASE) | Declared in the manifest with an honest `implemented` flag. Only FULL is implemented; it runs on every push. FAST and RELEASE record why they do not exist yet. |
| **Validation reuse and change-aware selection** | Permitted by policy, not used. Both are ways of not running something, so the validator enforces their preconditions — conservative default of `full`, no security-sensitive-path bypass, and reuse bound to the same tree, lockfile, toolchain and policy version. |

The FAST lane is deliberately **not** implemented. A change-aware lane may omit work only
against a versioned, tested impact map; no such map exists, the suite runs in seconds, so
there is nothing to gain and a real control to lose.

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
| `src/on_the_fly/domain/retention/` | The ten-second rule, enforced at runtime |
| `src/on_the_fly/domain/audio/` | Capture, voice activity detection, utterance segmentation |
| `src/on_the_fly/infrastructure/audio/` | Microphone adapter (ADR 0003); the only place PortAudio is imported |
| `src/on_the_fly/infrastructure/asr/` | Pinned model trust and the Whisper recogniser (ADR 0005) |
| `scripts/pin_model.py` | Produces a model pin for review; the loader never invents one |
| `src/on_the_fly/app/` | Composition root and CLI — where the retention store is wired in |
| `requirements.txt` | Runtime dependencies, each with a recorded admission decision |

## What is not enforced

Stated plainly, because the failure mode of governance work is believing it is finished:

- **The microphone adapter has captured real audio; the audio was not usable.** Run on a
  machine with input devices 2026-09-04: it opened a stream, yielded 100 frames of 640
  bytes at real-time cadence, reported zero overflows, and released the device on exit.
  Real ALSA open failures were mapped to `AudioDeviceError` with the device's own message,
  which had previously only been tested against a fake.

  What is still unverified is capture of *usable* audio. That machine's input produced 64%
  clipped samples with a −15838 DC offset, and raw `sounddevice` produced the same, so it
  is the hardware and not the adapter. Recognition from a live microphone remains untested.

- **The adapter requests 16 kHz and does not resample.** Both named hardware devices
  refused that rate; only the resampling system default accepted it. This is a real
  limitation on real hardware, and it fails loudly rather than silently.
- **No translation.** `Translator` is still a port with no implementation. Speech
  recognition now exists (ADR 0005) with weights pinned by digest and verified on load.
- **Recognition misses the performance budget by several times over** and the pipeline runs
  slower than real time. The pipeline is now written against a streaming interface
  (ADR 0006), but no engine that actually streams has been adopted. The language set is now
  decided (ADR 0007) and reduced to seven (ADR 0010).
- **Five of the six streaming languages are unmeasured.** Only English has a pinned
  streaming model and a measurement behind it. The other five are named on the strength of
  a published model existing, which is not the same as one having been adopted,
  licence-checked or tested. Russian was demoted to batch on exactly that check (ADR 0011).
- **No `Deleter` for a real spill location exists**, because nothing spills to disk yet.
- **The performance budget is still PROVISIONAL.** Segmentation is now measured (median
  0.018x real time over 9 runs), but the endpoint-to-caption targets cover stages that
  do not exist, and the measurement used synthetic tones rather than speech.
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
