"""Tests for the policy and governance validators.

Two kinds of test here, and both are needed. The positive tests assert that the manifests
committed to this repository actually pass. The negative tests assert that the validators
refuse a weakened manifest — because a validator that cannot fail is not a gate, and a
green suite that proves nothing is exactly what handbook 64S calls theatre.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import validate_coding_agent_policy as policy_validator
import validate_repository_governance as governance_validator

# scripts/ is placed on the import path by pythonpath in pyproject.toml.
REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------------------
# The manifests in this repository must pass.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    ["validate_coding_agent_policy.py", "validate_repository_governance.py"],
)
def test_validator_passes_against_this_repository(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"{script} failed:\n{result.stdout}\n{result.stderr}"


# --------------------------------------------------------------------------------------
# Negative tests: a weakened manifest must be rejected.
# --------------------------------------------------------------------------------------


def minimal_valid_retention() -> dict[str, Any]:
    return {
        "retention": {
            "default_post_use_seconds": 10,
            "require_explicit_classification": True,
            "deletion_failure_is_security_event": True,
            "classes": {
                "EPHEMERAL": {"default_post_use_seconds": 10},
                "OPERATIONAL_METADATA": {
                    "content_allowed": False,
                    "secrets_allowed": False,
                    "raw_media_allowed": False,
                },
                "DURABLE_PROJECT_ARTIFACT": {},
                "SECURITY_INCIDENT_HOLD": {},
            },
            "runtime_profiles": {},
        }
    }


def test_retention_baseline_is_accepted() -> None:
    errors: list[str] = []
    policy_validator.check_retention(minimal_valid_retention(), errors)
    assert errors == []


def test_retention_window_longer_than_ten_seconds_is_rejected() -> None:
    policy = minimal_valid_retention()
    policy["retention"]["default_post_use_seconds"] = 3600
    errors: list[str] = []
    policy_validator.check_retention(policy, errors)
    assert any("default_post_use_seconds" in error for error in errors)


def test_ephemeral_runtime_profile_above_the_window_is_rejected() -> None:
    policy = minimal_valid_retention()
    policy["retention"]["runtime_profiles"] = {
        "caption_scrollback": {"class": "EPHEMERAL", "post_use_seconds": 900},
    }
    errors: list[str] = []
    policy_validator.check_retention(policy, errors)
    assert any("caption_scrollback" in error for error in errors)


def test_operational_metadata_carrying_content_is_rejected() -> None:
    """The 30-day window is only defensible because this class holds no content."""
    policy = minimal_valid_retention()
    policy["retention"]["classes"]["OPERATIONAL_METADATA"]["content_allowed"] = True
    errors: list[str] = []
    policy_validator.check_retention(policy, errors)
    assert any("content_allowed" in error for error in errors)


def test_operational_metadata_profile_permitting_raw_content_is_rejected() -> None:
    policy = minimal_valid_retention()
    policy["retention"]["runtime_profiles"] = {
        "quality_metrics": {"class": "OPERATIONAL_METADATA", "raw_content_prohibited": False},
    }
    errors: list[str] = []
    policy_validator.check_retention(policy, errors)
    assert any("quality_metrics" in error for error in errors)


def test_unknown_retention_class_is_rejected() -> None:
    policy = minimal_valid_retention()
    policy["retention"]["runtime_profiles"] = {"mystery": {"class": "SOMEDAY_MAYBE"}}
    errors: list[str] = []
    policy_validator.check_retention(policy, errors)
    assert any("mystery" in error for error in errors)


def test_default_capability_grant_must_be_empty() -> None:
    errors: list[str] = []
    policy_validator.check_capabilities_and_isolation(
        {
            "capabilities": {
                "default_grants": ["WRITE_PROJECT"],
                "rules": ["never_infer_admin_from_repository_write_access"],
            },
            "project_isolation": {
                "cross_project_access_default": False,
                "stricter_policy_wins": True,
            },
        },
        errors,
    )
    assert any("default_grants" in error for error in errors)


def test_cross_project_access_on_by_default_is_rejected() -> None:
    errors: list[str] = []
    policy_validator.check_capabilities_and_isolation(
        {
            "capabilities": {
                "default_grants": [],
                "rules": ["never_infer_admin_from_repository_write_access"],
            },
            "project_isolation": {
                "cross_project_access_default": True,
                "stricter_policy_wins": True,
            },
        },
        errors,
    )
    assert any("cross_project_access_default" in error for error in errors)


def test_exception_without_an_expiry_field_is_rejected() -> None:
    errors: list[str] = []
    policy_validator.check_exceptions_and_invariants(
        {
            "exceptions": {
                "must_expire": True,
                "prohibit_silent_extension": True,
                "required_fields": ["owner", "reason"],
            },
            "security_invariants": sorted(policy_validator.REQUIRED_SECURITY_INVARIANTS),
        },
        errors,
    )
    assert any("expires_at" in error for error in errors)


def test_dropping_a_security_invariant_is_rejected() -> None:
    remaining = sorted(policy_validator.REQUIRED_SECURITY_INVARIANTS)[1:]
    errors: list[str] = []
    policy_validator.check_exceptions_and_invariants(
        {
            "exceptions": {
                "must_expire": True,
                "prohibit_silent_extension": True,
                "required_fields": sorted(policy_validator.REQUIRED_EXCEPTION_FIELDS),
            },
            "security_invariants": remaining,
        },
        errors,
    )
    assert any("security_invariants is missing" in error for error in errors)


# --------------------------------------------------------------------------------------
# Governance validator.
# --------------------------------------------------------------------------------------


def valid_main_branch() -> dict[str, Any]:
    return {
        "main_branch": {
            "protection_required": True,
            "pull_request_required": True,
            "direct_push_allowed": False,
            "force_push_allowed": False,
            "branch_deletion_allowed": False,
            "required_approvals": 1,
            "required_status_checks": ["quality"],
        }
    }


def test_branch_rule_baseline_is_accepted() -> None:
    errors: list[str] = []
    governance_validator.check_branch_rules(valid_main_branch(), errors)
    assert errors == []


@pytest.mark.parametrize(
    "field",
    ["direct_push_allowed", "force_push_allowed", "branch_deletion_allowed"],
)
def test_permitting_a_prohibited_branch_operation_is_rejected(field: str) -> None:
    governance = valid_main_branch()
    governance["main_branch"][field] = True
    errors: list[str] = []
    governance_validator.check_branch_rules(governance, errors)
    assert any(field in error for error in errors)


def test_dropping_the_quality_status_check_is_rejected() -> None:
    """Removing the required check is the CI evasion route handbook 64M prohibits."""
    governance = valid_main_branch()
    governance["main_branch"]["required_status_checks"] = []
    errors: list[str] = []
    governance_validator.check_branch_rules(governance, errors)
    assert any("quality" in error for error in errors)


def test_zero_approvals_without_a_documented_limitation_is_rejected() -> None:
    governance = valid_main_branch()
    governance["main_branch"]["required_approvals"] = 0
    errors: list[str] = []
    governance_validator.check_branch_rules(governance, errors)
    assert any("approval_limitation" in error for error in errors)


def test_zero_approvals_citing_an_unrecorded_exception_is_rejected() -> None:
    governance = valid_main_branch()
    governance["main_branch"]["required_approvals"] = 0
    governance["approval_limitation"] = {
        "exception_record": "EXC-9999-01-01-999",
        "compensating_controls": ["something"],
        "removal_condition": "someday",
    }
    errors: list[str] = []
    governance_validator.check_branch_rules(governance, errors)
    assert any("EXC-9999-01-01-999" in error for error in errors)


def test_codeowners_parser_ignores_comments_and_blank_lines() -> None:
    parsed = governance_validator.parse_codeowners_patterns(
        "# a comment\n\n/docs/ @tehki\n/scripts/thing.py @tehki  # trailing comment\n@nopath\n"
    )
    assert parsed == ["/docs/", "/scripts/thing.py"]


# ---------------------------------------------------------------------------------------
# Article 15 authorization boundaries, added upstream in Constitution 1.3.
#
# All three controls are ways of not asking for approval again, so each one is only safe
# while its stated preconditions hold. These tests remove a precondition at a time and
# assert the validator objects — a gate that cannot fail is not a gate.
# ---------------------------------------------------------------------------------------


def valid_authorization_boundaries() -> dict[str, Any]:
    return {
        "development_velocity": {
            "authorization_reuse": {
                "allowed": True,
                "requires_unchanged": [
                    "project",
                    "target_or_resource",
                    "exact_head_or_version_when_specified",
                    "scope",
                    "risk_class",
                    "capability_class",
                    "side_effect_class",
                    "rollback_or_recovery_assumptions",
                    "expiry_or_exception_state",
                ],
                "new_authorization_required_on_material_change": True,
                "privileged_or_destructive_exact_target_reverification_still_required": True,
            },
            "delivery_boundaries": {
                "merge_and_runtime_activation_separate_by_default": True,
                "merge_authorization_does_not_imply_runtime_activation": True,
                "deployment_requires_separately_authorized_boundary_unless_explicitly_combined": (
                    True
                ),
            },
            "preauthorized_rollback": {
                "may_execute_without_second_approval_when_exact_condition_was_authorized": True,
                "must_stay_within_exact_target_and_method": True,
                "verify_restored_state": True,
                "report_trigger_and_result": True,
                "do_not_retry_failed_mutation_indefinitely": True,
            },
            "stop_conditions": ["authorized_exact_head_or_target_changed"],
        }
    }


def test_authorization_boundary_baseline_is_accepted() -> None:
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(valid_authorization_boundaries(), errors)
    assert errors == []


def test_narrowing_what_authorization_reuse_requires_is_rejected() -> None:
    """Dropping an input from the list widens reuse without saying so."""
    policy = valid_authorization_boundaries()
    reuse = policy["development_velocity"]["authorization_reuse"]
    reuse["requires_unchanged"] = [
        item for item in reuse["requires_unchanged"] if item != "risk_class"
    ]
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any("risk_class" in error for error in errors)


def test_reuse_surviving_a_material_change_is_rejected() -> None:
    policy = valid_authorization_boundaries()
    policy["development_velocity"]["authorization_reuse"][
        "new_authorization_required_on_material_change"
    ] = False
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any("material_change" in error for error in errors)


def test_reused_authorization_skipping_destructive_reverification_is_rejected() -> None:
    """Article 10 is not waived by Article 15. A reused approval is still not a re-read."""
    policy = valid_authorization_boundaries()
    policy["development_velocity"]["authorization_reuse"][
        "privileged_or_destructive_exact_target_reverification_still_required"
    ] = False
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any("reverification" in error for error in errors)


@pytest.mark.parametrize(
    "field",
    [
        "merge_and_runtime_activation_separate_by_default",
        "merge_authorization_does_not_imply_runtime_activation",
        "deployment_requires_separately_authorized_boundary_unless_explicitly_combined",
    ],
)
def test_collapsing_a_delivery_boundary_is_rejected(field: str) -> None:
    """Merging a change is not permission to run it, and neither is permission to ship it."""
    policy = valid_authorization_boundaries()
    policy["development_velocity"]["delivery_boundaries"][field] = False
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any(field in error for error in errors)


@pytest.mark.parametrize(
    "field",
    [
        "must_stay_within_exact_target_and_method",
        "verify_restored_state",
        "report_trigger_and_result",
        "do_not_retry_failed_mutation_indefinitely",
    ],
)
def test_unattended_rollback_without_its_guards_is_rejected(field: str) -> None:
    """A rollback that may run without approval is a mutation that may run without approval."""
    policy = valid_authorization_boundaries()
    policy["development_velocity"]["preauthorized_rollback"][field] = False
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any(field in error for error in errors)


def test_rollback_guards_are_not_required_when_rollback_is_not_preauthorized() -> None:
    """The guards exist because the rollback is unattended. No unattended rollback, no guards."""
    policy = valid_authorization_boundaries()
    rollback = policy["development_velocity"]["preauthorized_rollback"]
    unattended = "may_execute_without_second_approval_when_exact_condition_was_authorized"
    rollback[unattended] = False
    rollback["verify_restored_state"] = False
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert errors == []


def test_omitting_stop_conditions_is_rejected() -> None:
    policy = valid_authorization_boundaries()
    del policy["development_velocity"]["stop_conditions"]
    errors: list[str] = []
    policy_validator.check_authorization_boundaries(policy, errors)
    assert any("stop_conditions" in error for error in errors)
