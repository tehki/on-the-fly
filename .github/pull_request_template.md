## What and why

<!-- The behaviour that changes, and the behaviour deliberately preserved. -->

## Risk classification

<!-- Pick one. Do not classify downward to avoid a safeguard (Constitution Article 9). -->

- [ ] LOW - read-only, docs without security effect, or test-only
- [ ] MODERATE - dependency update, refactor, non-production configuration
- [ ] HIGH - deployment, data deletion, schema migration, credential change, privileged side effect
- [ ] CRITICAL - destructive production operation, protected-branch change, security control disabled, irreversible infrastructure change

HIGH and CRITICAL additionally require: exact-target verification, blast-radius
assessment, and a rollback or recovery plan. CRITICAL also requires dry-run or
reversible-alternative review and post-action verification. Independent review is
recorded as unsupported while the repository has a single maintainer
(EXC-2026-09-01-001).

## Capabilities used

<!-- Minimum set only. Ordinary write access is never ADMIN (Constitution Article 3). -->

- [ ] READ_PROJECT
- [ ] WRITE_PROJECT
- [ ] EXECUTE_LOCAL
- [ ] NETWORK_EXTERNAL
- [ ] INSTALL_DEPENDENCY
- [ ] DEPLOY
- [ ] DELETE
- [ ] MANAGE_SECRETS
- [ ] ADMIN

## Retention

- [ ] No project content is newly persisted by this change
- [ ] Every new data path has an explicit retention class
- [ ] Transient content keeps the 10-second post-use default
- [ ] Any `OPERATIONAL_METADATA` added contains metadata only - no bodies, media or secrets
- [ ] Longer retention, if any, is recorded in `docs/EXCEPTIONS.md` with owner and expiry

## Security

- [ ] Authorization precedes any protected side effect
- [ ] Inputs validated and canonicalised; no new injection, traversal, SSRF or unsafe deserialization path
- [ ] No secret in source, logs, tests, telemetry or fixtures
- [ ] TLS/certificate verification not weakened
- [ ] Security failures fail closed
- [ ] New dependencies passed admission review (need, provenance, footprint, licence, install scripts)

## Destructive actions

- [ ] Not applicable
- [ ] Exact target re-resolved immediately before execution
- [ ] Blast radius assessed
- [ ] Dry-run or reversible alternative considered
- [ ] Rollback path defined
- [ ] Actual result verified and reported

## Performance

- [ ] Not performance-sensitive
- [ ] Representative baseline recorded, or its absence explained
- [ ] Target metric and success condition stated
- [ ] Before/after compare equivalent workloads
- [ ] Resource effects reviewed (CPU, memory, I/O, network, queue depth)
- [ ] No required CI gate was renamed, skipped or weakened for speed

## Verification

<!-- What you actually ran, and its actual result. Do not state that a check passed
     unless it was executed (Constitution Article 2, handbook 52). -->

## Claims about remote controls

- [ ] This PR makes no claim about branch protection, deletion, encryption or isolation
- [ ] Any such claim here was independently verified, and the verification is shown above
