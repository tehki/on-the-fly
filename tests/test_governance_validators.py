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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_coding_agent_policy as policy_validator  # noqa: E402
import validate_repository_governance as governance_validator  # noqa: E402


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
