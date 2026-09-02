#!/usr/bin/env python3
"""Validate the Repository Governance manifest against the repository it governs.

Governance-as-code is only worth having if it is true. The v1.0 manifest inherited by
this project protected a handbook path that had already been superseded, so the file it
named did not exist and the file that did exist was unguarded. Every check here exists to
make that class of drift fail the build.

This validator says nothing about remote branch protection. Local manifests are not
provider-side enforcement (Constitution Article 11); remote state is verified separately
and recorded in docs/GITHUB_REPOSITORY_GOVERNANCE.md.

Exit code 0 = manifest is internally consistent and true of this tree. 1 = it is not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

GOVERNANCE_FILE = REPO_ROOT / "REPOSITORY_GOVERNANCE_v1.1-otf1.yaml"
POLICY_FILE = REPO_ROOT / "CODING_AGENT_POLICY_v1.2-otf1.yaml"
CODEOWNERS_FILE = REPO_ROOT / ".github" / "CODEOWNERS"
EXCEPTIONS_FILE = REPO_ROOT / "docs" / "EXCEPTIONS.md"

REQUIRED_STATUS_CHECK = "quality"


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a manifest with safe_load only. See validate_coding_agent_policy.load_policy."""
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"{path.name}: expected a mapping at the document root")
    return data


def check_branch_rules(governance: dict[str, Any], errors: list[str]) -> None:
    main_branch = governance.get("main_branch", {})

    if main_branch.get("protection_required") is not True:
        errors.append("main_branch.protection_required must be true (Constitution Article 11)")
    if main_branch.get("pull_request_required") is not True:
        errors.append("main_branch.pull_request_required must be true (Article 11)")

    for forbidden in ("direct_push_allowed", "force_push_allowed", "branch_deletion_allowed"):
        if main_branch.get(forbidden) is not False:
            errors.append(f"main_branch.{forbidden} must be false (Article 11)")

    checks = main_branch.get("required_status_checks") or []
    if REQUIRED_STATUS_CHECK not in checks:
        errors.append(
            f"main_branch.required_status_checks must include {REQUIRED_STATUS_CHECK!r} "
            "(Article 11)"
        )

    # A zero-approval requirement is permitted only as the documented limitation that
    # Article 9 provides for, never as a silent downgrade. It must name a real exception.
    if main_branch.get("required_approvals") == 0:
        limitation = governance.get("approval_limitation")
        if not limitation:
            errors.append(
                "main_branch.required_approvals is 0 but no approval_limitation block "
                "explains why (Constitution Article 9 forbids a silent downgrade)"
            )
        else:
            record = limitation.get("exception_record")
            if not record:
                errors.append("approval_limitation.exception_record is missing")
            elif not EXCEPTIONS_FILE.exists():
                errors.append(
                    f"approval_limitation cites {record} but {EXCEPTIONS_FILE.name} does not exist"
                )
            elif record not in EXCEPTIONS_FILE.read_text(encoding="utf-8"):
                errors.append(
                    f"approval_limitation cites {record}, which is not recorded in "
                    f"docs/{EXCEPTIONS_FILE.name}"
                )
            if not limitation.get("compensating_controls"):
                errors.append("approval_limitation.compensating_controls must not be empty")
            if not limitation.get("removal_condition"):
                errors.append("approval_limitation.removal_condition must be stated (Article 13)")


def parse_codeowners_patterns(text: str) -> list[str]:
    """Return the path patterns declared in a CODEOWNERS file, ignoring comments."""
    patterns: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) >= 2:
            patterns.append(fields[0])
    return patterns


def check_sensitive_paths(governance: dict[str, Any], errors: list[str]) -> None:
    """Every protected path must exist, and must actually be owned by someone.

    A protected path that does not exist protects nothing, and a path protected in the
    manifest but absent from CODEOWNERS is a governance claim with no mechanism behind it.
    """
    section = governance.get("security_sensitive_paths", {})
    declared_paths = section.get("paths") or []
    if not declared_paths:
        errors.append("security_sensitive_paths.paths must not be empty")
        return

    if not CODEOWNERS_FILE.exists():
        errors.append(".github/CODEOWNERS does not exist but sensitive paths are declared")
        codeowner_patterns: list[str] = []
    else:
        codeowner_patterns = parse_codeowners_patterns(CODEOWNERS_FILE.read_text(encoding="utf-8"))

    for declared in declared_paths:
        relative = str(declared).lstrip("/")
        target = REPO_ROOT / relative
        is_directory_rule = str(declared).endswith("/")

        if not target.exists():
            if is_directory_rule:
                # A directory rule may legitimately precede the code it will govern, but
                # it must be visible as pending rather than silently vacuous.
                errors.append(
                    f"security_sensitive_paths: {declared} does not exist yet. Create the "
                    "directory (a .gitkeep is enough) or remove the rule; a protected path "
                    "that does not exist protects nothing."
                )
            else:
                errors.append(
                    f"security_sensitive_paths: {declared} does not exist in the repository"
                )

        if declared not in codeowner_patterns:
            errors.append(f"security_sensitive_paths: {declared} has no CODEOWNERS rule")


def check_ci_wiring(governance: dict[str, Any], errors: list[str]) -> None:
    ci = governance.get("ci", {})

    workflow_name = ci.get("workflow")
    if not workflow_name:
        errors.append("ci.workflow is missing")
    else:
        workflow_path = REPO_ROOT / str(workflow_name)
        if not workflow_path.exists():
            errors.append(f"ci.workflow points at missing file {workflow_name!r}")
        else:
            workflow_text = workflow_path.read_text(encoding="utf-8")
            required_job = str(ci.get("required_job") or REQUIRED_STATUS_CHECK)
            # The required status check is matched by job name. If the job is renamed the
            # remote check silently stops being satisfied, which is the evasion route
            # handbook 64M prohibits.
            if not re.search(rf"^\s+{re.escape(required_job)}:\s*$", workflow_text, re.MULTILINE):
                errors.append(
                    f"ci.required_job {required_job!r} is not defined as a job in {workflow_name}"
                )

    for field in ("policy_validator", "governance_validator", "main_push_provenance_script"):
        script_name = ci.get(field)
        if not script_name:
            errors.append(f"ci.{field} is missing")
        elif not (REPO_ROOT / str(script_name)).exists():
            errors.append(f"ci.{field} points at missing file {script_name!r}")


def check_development_flow(governance: dict[str, Any], errors: list[str]) -> None:
    """Constitution Article 15, as this repository applies it.

    Batching commits into one coherent work-unit pull request is allowed. The two rules
    that stop it becoming a way to slip work past review are checked here: the highest
    included risk governs the batch, and independent objectives stay separate.
    """
    flow = governance.get("development_flow", {})
    if not flow:
        errors.append("development_flow is missing; Article 15 requires a stated position")
        return

    if flow.get("mixed_risk_batch_uses_highest_risk") is not True:
        errors.append("development_flow.mixed_risk_batch_uses_highest_risk must be true")
    if flow.get("separate_pull_request_required_for_unrelated_objectives") is not True:
        errors.append(
            "development_flow.separate_pull_request_required_for_unrelated_objectives must be true"
        )
    separate_privileged = flow.get(
        "separate_pull_request_required_for_independent_privileged_or_destructive_authorization_boundary"
    )
    if separate_privileged is not True:
        errors.append(
            "development_flow: an independent privileged or destructive authorization "
            "boundary must require its own pull request"
        )
    if flow.get("cross_project_batching_default_allowed") is not False:
        errors.append(
            "development_flow.cross_project_batching_default_allowed must be false "
            "(Constitution Article 7)"
        )


def check_validation_lanes(governance: dict[str, Any], errors: list[str]) -> None:
    """A declared lane must be honest about whether it exists.

    Article 15 permits FAST and RELEASE lanes; it does not require them. What it does not
    permit is a manifest describing acceleration this repository has not built, because a
    reader would take that for an enforced control. So a lane is either implemented, or it
    says why it is not.
    """
    lanes = governance.get("ci", {}).get("validation_lanes", {})
    if not lanes:
        errors.append("ci.validation_lanes is missing; state which lanes exist")
        return

    full = lanes.get("FULL", {})
    if full.get("implemented") is not True:
        errors.append(
            "ci.validation_lanes.FULL.implemented must be true; a repository without a "
            "full validation lane has no acceptance gate"
        )
    for required in (
        "required_for_security_sensitive_paths",
        "required_for_dependency_or_lockfile_changes",
        "required_for_ci_or_governance_changes",
        "required_for_high_or_critical_risk",
        "required_when_change_impact_is_ambiguous",
    ):
        if full.get(required) is not True:
            errors.append(f"ci.validation_lanes.FULL.{required} must be true")

    for name in ("FAST", "RELEASE"):
        lane = lanes.get(name, {})
        if lane and lane.get("implemented") is not True and not lane.get("not_implemented_reason"):
            errors.append(
                f"ci.validation_lanes.{name} is declared but not implemented and gives no "
                "reason; an unimplemented lane reads as a control that exists"
            )

    if lanes.get("FAST", {}).get("unknown_relevance_falls_back_to_full") is not True:
        errors.append("ci.validation_lanes.FAST.unknown_relevance_falls_back_to_full must be true")

    acceleration = governance.get("ci", {}).get("acceleration", {})
    if acceleration.get("full_gate_semantics_must_not_be_reduced") is not True:
        errors.append("ci.acceleration.full_gate_semantics_must_not_be_reduced must be true")
    reuse_binding = acceleration.get(
        "same_source_validation_reuse_requires_input_and_artifact_integrity_binding"
    )
    reuse_allowed = acceleration.get("same_source_validation_reuse_allowed") is True
    if reuse_allowed and reuse_binding is not True:
        errors.append(
            "ci.acceleration permits validation reuse without requiring integrity binding; "
            "reused evidence must be bound to the same inputs (Article 15)"
        )

    merge_queue = governance.get("ci", {}).get("merge_queue", {})
    if merge_queue and merge_queue.get("full_gate_required_on_merge_group") is not True:
        errors.append("ci.merge_queue.full_gate_required_on_merge_group must be true")


def check_truthfulness(governance: dict[str, Any], errors: list[str]) -> None:
    truthfulness = governance.get("truthfulness", {})
    for required in (
        "never_claim_remote_branch_protection_without_verification",
        "local_ci_manifest_does_not_equal_remote_enforcement",
    ):
        if truthfulness.get(required) is not True:
            errors.append(f"truthfulness.{required} must be true (Constitution Article 2)")

    control_plane = governance.get("external_control_plane", {})
    if control_plane.get("branch_protection_or_ruleset_required") is not True:
        errors.append("external_control_plane.branch_protection_or_ruleset_required must be true")

    # While main is unprotected the compensating detection must be declared AND wired.
    last_verified = truthfulness.get("last_verified_remote_state", {})
    if last_verified.get("branch_protection_present") is False:
        detection_required = control_plane.get(
            "compensating_main_push_detection_required_while_unprotected"
        )
        if detection_required is not True:
            errors.append(
                "remote branch protection is recorded as absent, so "
                "compensating_main_push_detection_required_while_unprotected must be true"
            )
        if governance.get("ci", {}).get("main_push_provenance_detection_required") is not True:
            errors.append(
                "remote branch protection is recorded as absent, so "
                "ci.main_push_provenance_detection_required must be true"
            )


def check_cross_document_versions(governance: dict[str, Any], errors: list[str]) -> None:
    """The governance manifest and the policy must agree about which documents are active."""
    if not POLICY_FILE.exists():
        errors.append(f"{POLICY_FILE.name} not found; cannot cross-check versions")
        return

    policy = load_yaml(POLICY_FILE)
    policy_meta = policy.get("policy", {})
    governance_meta = governance.get("governance", {})

    declared_manifest = policy.get("repository_governance", {}).get("manifest")
    if declared_manifest != GOVERNANCE_FILE.name:
        errors.append(
            f"policy repository_governance.manifest is {declared_manifest!r} but this "
            f"validator governs {GOVERNANCE_FILE.name}"
        )

    pairs = (
        ("policy_version", policy_meta.get("version")),
        ("constitution_version", policy_meta.get("constitution_version")),
        ("handbook_version", policy_meta.get("handbook_version")),
    )
    for field, policy_value in pairs:
        governance_value = governance_meta.get(field)
        if governance_value != policy_value:
            errors.append(
                f"version drift: governance.{field} is {governance_value!r} but the policy "
                f"declares {policy_value!r}"
            )

    governance_version = governance_meta.get("version")
    policy_governance_version = policy_meta.get("repository_governance_version")
    if governance_version != policy_governance_version:
        errors.append(
            f"version drift: governance.version is {governance_version!r} but the policy "
            f"declares repository_governance_version {policy_governance_version!r}"
        )


def main() -> int:
    if not GOVERNANCE_FILE.exists():
        print(f"FAIL {GOVERNANCE_FILE.name} not found", file=sys.stderr)
        return 1

    governance = load_yaml(GOVERNANCE_FILE)
    errors: list[str] = []

    check_branch_rules(governance, errors)
    check_development_flow(governance, errors)
    check_validation_lanes(governance, errors)
    check_sensitive_paths(governance, errors)
    check_ci_wiring(governance, errors)
    check_truthfulness(governance, errors)
    check_cross_document_versions(governance, errors)

    if errors:
        print(f"FAIL repository governance validation: {len(errors)} violation(s)", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"PASS repository governance validation: {GOVERNANCE_FILE.name} is consistent with "
        "this tree (this says nothing about remote enforcement)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
