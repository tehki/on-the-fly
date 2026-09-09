#!/usr/bin/env python3
"""Read the branch ruleset back from GitHub and compare it against the manifest.

`REPOSITORY_GOVERNANCE_v1.2-otf1.yaml` declares what protection `main` is under.
`scripts/validate_repository_governance.py` checks that the declaration is consistent with
this tree and says so in its own success line: *this says nothing about remote enforcement.*
Constitution Article 2 forbids claiming the remote is enforced without verifying it, so the
verification has been performed by hand and written into
`docs/GITHUB_REPOSITORY_GOVERNANCE.md` — twice, on 2026-09-01 and 2026-09-07.

**A verification performed by hand is a verification nobody can repeat.** It also missed
things: the 2026-09-07 table records four fields of the pull-request rule and the remote
carries six, one of which — `allowed_merge_methods` — is the control that keeps the linear
history the manifest requires. Neither omission was a drift; both were a reading.

So the comparison is derived from the manifest here instead, field by field, and prints what
it read. It needs the `gh` CLI authenticated against the repository and therefore does not
run in CI: a check that needed a token with this reach in CI would be a worse control than
the one it verifies.

```bash
python scripts/verify_branch_protection.py
python scripts/verify_branch_protection.py --json   # for pasting into the record
```

Exit status is 0 when every declared field matches, 1 when any does not, and 2 when the
remote could not be read at all — which is not a pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_repository_governance import (  # noqa: E402
    GOVERNANCE_FILE,
    load_yaml,
)

# What a rule's *presence* asserts. A ruleset expresses "force pushes are refused" by
# carrying a `non_fast_forward` rule at all, so the manifest's booleans map to whether a rule
# type is in the list rather than to any parameter of it.
PRESENCE_RULES = {
    "force_push_allowed": ("non_fast_forward", False),
    "branch_deletion_allowed": ("deletion", False),
    "direct_push_allowed": ("pull_request", False),
    "pull_request_required": ("pull_request", True),
    "require_linear_history": ("required_linear_history", True),
}

# Manifest field -> (rule type, parameter). Straight comparisons of a declared value.
PARAMETER_RULES = {
    "required_approvals": ("pull_request", "required_approving_review_count"),
    "require_code_owner_review": ("pull_request", "require_code_owner_review"),
    "dismiss_stale_approvals": ("pull_request", "dismiss_stale_reviews_on_push"),
    "require_conversation_resolution": ("pull_request", "required_review_thread_resolution"),
    "require_up_to_date_before_merge": (
        "required_status_checks",
        "strict_required_status_checks_policy",
    ),
    "allowed_merge_methods": ("pull_request", "allowed_merge_methods"),
    "require_extra_approval_for_unattributed_changes": (
        "pull_request",
        "require_extra_approval_for_unattributed_changes",
    ),
}

# Declared in the manifest for the record rather than as a rule to compare: they describe why
# a value is what it is, and `check_exception_references_exist` already ties them to the
# register.
NOT_REMOTE_SETTINGS = {
    "protection_required",
    "required_approvals_limitation",
    "require_code_owner_review_limitation",
}


class RemoteUnavailableError(RuntimeError):
    """The remote could not be read. Never reported as a pass."""


def gh_json(path: str) -> Any:
    try:
        result = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
    except FileNotFoundError as exc:
        raise RemoteUnavailableError("the gh CLI is not installed") from exc
    if result.returncode != 0:
        raise RemoteUnavailableError(f"gh api {path} failed: {result.stderr.strip()[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteUnavailableError(f"gh api {path} returned no JSON") from exc


def repository() -> str:
    manifest = load_yaml(REPO_ROOT / GOVERNANCE_FILE.name)
    name = manifest["governance"].get("repository")
    if not name:
        raise RemoteUnavailableError("the manifest declares no repository to check")
    return str(name)


def active_ruleset(repo: str) -> dict[str, Any]:
    """The one ruleset covering the default branch, read in full.

    More than one is not an error here but it is reported: rules from several rulesets
    combine, and a comparison against one of them would describe less than what is enforced.
    """
    listing = gh_json(f"repos/{repo}/rulesets")
    if not listing:
        raise RemoteUnavailableError("no rulesets exist on this repository")
    if len(listing) > 1:
        names = ", ".join(str(item.get("name")) for item in listing)
        raise RemoteUnavailableError(f"more than one ruleset applies and this reads one: {names}")
    return dict(gh_json(f"repos/{repo}/rulesets/{listing[0]['id']}"))


def rules_by_type(ruleset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["type"]: dict(rule.get("parameters") or {}) for rule in ruleset.get("rules", [])}


def compare(declared: dict[str, Any], ruleset: dict[str, Any]) -> list[str]:
    """Every mismatch, as a line a reader can act on. Empty means the remote matches."""
    rules = rules_by_type(ruleset)
    failures: list[str] = []

    if ruleset.get("enforcement") != "active":
        failures.append(f"ruleset enforcement is {ruleset.get('enforcement')!r}, not 'active'")
    if ruleset.get("bypass_actors"):
        failures.append(f"bypass_actors is not empty: {ruleset['bypass_actors']}")
    included = (ruleset.get("conditions") or {}).get("ref_name", {}).get("include", [])
    if included != ["~DEFAULT_BRANCH"]:
        failures.append(f"the ruleset does not target only the default branch: {included}")

    for field, (rule_type, expected_presence) in PRESENCE_RULES.items():
        if field not in declared:
            continue
        # `force_push_allowed: false` means the rule that refuses force pushes must exist.
        wanted = expected_presence if declared[field] else not expected_presence
        if (rule_type in rules) is not wanted:
            state = "present" if rule_type in rules else "absent"
            failures.append(f"{field}={declared[field]!r} needs rule {rule_type!r}; it is {state}")

    for field, (rule_type, parameter) in PARAMETER_RULES.items():
        if field not in declared:
            continue
        if rule_type not in rules:
            failures.append(f"{field} cannot be checked: rule {rule_type!r} is absent")
            continue
        actual = rules[rule_type].get(parameter)
        if actual != declared[field]:
            failures.append(
                f"{field}: manifest says {declared[field]!r}, remote {parameter}={actual!r}"
            )

    checks = declared.get("required_status_checks")
    if checks is not None:
        remote_checks = [
            check.get("context")
            for check in rules.get("required_status_checks", {}).get("required_status_checks", [])
        ]
        if sorted(remote_checks) != sorted(checks):
            failures.append(f"required_status_checks: manifest {checks}, remote {remote_checks}")

    return failures


def unrecorded(declared: dict[str, Any], ruleset: dict[str, Any]) -> list[str]:
    """Rules and parameters the remote enforces that the manifest never mentions.

    Not failures. The manifest is a floor, and the remote holding more than it declares is
    the safe direction — but a control nobody wrote down is a control nobody is maintaining,
    and `allowed_merge_methods` reached this repository without ever being recorded.
    """
    rules = rules_by_type(ruleset)
    described = {rule for rule, _ in PRESENCE_RULES.values()} | {
        rule for rule, _ in PARAMETER_RULES.values()
    }
    described.add("required_status_checks")

    notes = [
        f"rule {name!r} is enforced and the manifest does not mention it"
        for name in sorted(set(rules) - described)
    ]
    compared = {parameter for _, parameter in PARAMETER_RULES.values()}
    for name in sorted(described & set(rules)):
        for parameter, value in sorted(rules[name].items()):
            if parameter not in compared and parameter != "required_status_checks":
                notes.append(f"{name}.{parameter} = {value!r} is enforced and not declared")
    return notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_branch_protection",
        description="Compare the remote branch ruleset against the governance manifest.",
    )
    parser.add_argument("--json", action="store_true", help="print the ruleset as read")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = repository()
        ruleset = active_ruleset(repo)
    except RemoteUnavailableError as exc:
        print(f"UNVERIFIED remote branch protection: {exc}", file=sys.stderr)
        print("This is not a pass. Nothing about the remote has been established.")
        return 2

    if args.json:
        print(json.dumps(ruleset, indent=2, sort_keys=True))

    declared = load_yaml(REPO_ROOT / GOVERNANCE_FILE.name)["main_branch"]
    unchecked = sorted(
        set(declared)
        - set(PRESENCE_RULES)
        - set(PARAMETER_RULES)
        - {"required_status_checks"}
        - NOT_REMOTE_SETTINGS
    )
    failures = compare(declared, ruleset)

    print(f"repository    {repo}")
    print(
        f"ruleset       {ruleset.get('name')!r} id {ruleset.get('id')} {ruleset.get('enforcement')}"
    )
    print(f"rules         {', '.join(sorted(rules_by_type(ruleset)))}")
    for note in unrecorded(declared, ruleset):
        print(f"  not declared: {note}")
    for field in unchecked:
        print(f"  not checked : {field} has no remote equivalent in this script")

    if failures:
        print(f"\nFAIL remote branch protection: {len(failures)} mismatch(es)")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nPASS remote branch protection matches the manifest (read back just now)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
