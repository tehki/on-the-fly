"""Tests for the checks added when the upstream v1.5 stack was adopted.

Constitution v1.2 added Article 15 (engineering throughput without control dilution) and
Article 16 (cryptographic protection and key separation). Both introduce ways to go faster
or to store less plainly, and both come with conditions. These tests exist to prove the
conditions are enforced rather than merely written down.

Kept separate from `test_governance_validators.py` so the v1.5 surface is reviewable on its
own; both files exercise the same two validators.
"""

from __future__ import annotations

from typing import Any

import validate_coding_agent_policy as policy_validator
import validate_repository_governance as governance_validator

# scripts/ is on the import path via pythonpath in pyproject.toml.


# ======================================================================================
# Article 16 — cryptography
# ======================================================================================


def valid_cryptography() -> dict[str, Any]:
    return {
        "cryptography": {
            "custom_crypto_prohibited": True,
            "maintained_library_required": True,
            "secure_randomness_required_for_secrets_tokens_keys_nonces": True,
            "authenticated_encryption_required_for_new_application_level_encryption": True,
            "encryption_does_not_extend_retention_or_authorization": True,
            "key_reuse_across_unrelated_purposes_prohibited": True,
            "transport": {
                "tls_certificate_and_hostname_verification_required": True,
                "privileged_or_non_idempotent_zero_rtt_prohibited": True,
            },
            "at_rest": {
                "nonce_uniqueness_required": True,
                "deterministic_encryption_default_allowed": False,
                "ephemeral_disk_spill_requires_encryption_and_normal_retention_expiry": True,
            },
            "passwords": {
                "reversible_encryption_default_allowed": False,
                "preferred_hash": "Argon2id",
            },
            "key_management": {
                (
                    "plaintext_private_keys_in_source_config_logs_ci_artifacts_or_diagnostics_prohibited"
                ): True,
                "key_and_ciphertext_separation_required": True,
            },
        }
    }


def test_cryptography_baseline_is_accepted() -> None:
    errors: list[str] = []
    policy_validator.check_cryptography(valid_cryptography(), errors)
    assert errors == []


def test_disabling_tls_verification_is_rejected() -> None:
    policy = valid_cryptography()
    policy["cryptography"]["transport"]["tls_certificate_and_hostname_verification_required"] = (
        False
    )
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("tls_certificate_and_hostname" in error for error in errors)


def test_encryption_may_not_be_used_to_extend_retention() -> None:
    """The most tempting way to launder both retention and authorisation at once."""
    policy = valid_cryptography()
    policy["cryptography"]["encryption_does_not_extend_retention_or_authorization"] = False
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("encryption_does_not_extend_retention" in error for error in errors)


def test_deterministic_encryption_by_default_is_rejected() -> None:
    """Deterministic ciphertext leaks equality, which is enough to identify a repeat."""
    policy = valid_cryptography()
    policy["cryptography"]["at_rest"]["deterministic_encryption_default_allowed"] = True
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("deterministic" in error for error in errors)


def test_encrypted_disk_spill_still_expires() -> None:
    """Encrypting a spilled audio buffer does not exempt it from the ten-second rule."""
    policy = valid_cryptography()
    policy["cryptography"]["at_rest"][
        "ephemeral_disk_spill_requires_encryption_and_normal_retention_expiry"
    ] = False
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("ephemeral_disk_spill" in error for error in errors)


def test_reversible_password_storage_is_rejected() -> None:
    policy = valid_cryptography()
    policy["cryptography"]["passwords"]["reversible_encryption_default_allowed"] = True
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("reversible_encryption" in error for error in errors)


def test_plaintext_private_keys_in_diagnostics_are_rejected() -> None:
    policy = valid_cryptography()
    policy["cryptography"]["key_management"][
        "plaintext_private_keys_in_source_config_logs_ci_artifacts_or_diagnostics_prohibited"
    ] = False
    errors: list[str] = []
    policy_validator.check_cryptography(policy, errors)
    assert any("plaintext_private_keys" in error for error in errors)


# ======================================================================================
# Article 15 — batching without diluting a review boundary
# ======================================================================================


def valid_velocity() -> dict[str, Any]:
    return {
        "development_velocity": {
            "mixed_risk_batch_uses_highest_risk": True,
            "full_validation_required_before_ready_merge_state": True,
            "batch_conditions": ["single_project_boundary"],
            "separate_review_boundary_required_for": [
                "unrelated_objectives",
                "unauthorized_cross_project_changes",
                "independent_destructive_or_privileged_authorization_boundaries",
                "incompatible_confidentiality_or_retention_boundaries",
            ],
        }
    }


def test_development_velocity_baseline_is_accepted() -> None:
    errors: list[str] = []
    policy_validator.check_development_velocity(valid_velocity(), errors)
    assert errors == []


def test_a_batch_may_not_be_scored_below_its_highest_risk() -> None:
    """Otherwise batching becomes a way to slip a HIGH change in beside a LOW one."""
    policy = valid_velocity()
    policy["development_velocity"]["mixed_risk_batch_uses_highest_risk"] = False
    errors: list[str] = []
    policy_validator.check_development_velocity(policy, errors)
    assert any("highest" in error for error in errors)


def test_a_merge_candidate_may_not_skip_full_validation() -> None:
    policy = valid_velocity()
    policy["development_velocity"]["full_validation_required_before_ready_merge_state"] = False
    errors: list[str] = []
    policy_validator.check_development_velocity(policy, errors)
    assert any("full_validation_required" in error for error in errors)


def test_dropping_a_required_separate_review_boundary_is_rejected() -> None:
    policy = valid_velocity()
    policy["development_velocity"]["separate_review_boundary_required_for"] = [
        "unrelated_objectives"
    ]
    errors: list[str] = []
    policy_validator.check_development_velocity(policy, errors)
    assert any("separate_review_boundary_required_for is missing" in error for error in errors)


# ======================================================================================
# Article 15 — a faster lane is not a weaker gate
# ======================================================================================


def valid_acceleration() -> dict[str, Any]:
    return {
        "ci_acceleration": {
            "change_aware_selection": {
                "conservative_default": "full",
                "mapping_must_be_versioned_and_tested": True,
                "security_sensitive_path_bypass_prohibited": True,
                "omitted_gate_must_be_provably_irrelevant": True,
            },
            "validation_lanes": {
                "FAST": {"unknown_relevance_falls_back_to_full": True},
                "FULL": {"required_for": ["security_sensitive_paths"]},
            },
            "validation_reuse": {
                "same_source_or_tree_only": True,
                "same_dependency_lock_and_toolchain_identity_required": True,
                "same_policy_and_governance_version_required": True,
                "source_or_relevant_policy_change_invalidates_result": True,
                "cache_miss_remains_supported": True,
            },
        }
    }


def test_ci_acceleration_baseline_is_accepted() -> None:
    errors: list[str] = []
    policy_validator.check_ci_acceleration(valid_acceleration(), errors)
    assert errors == []


def test_change_aware_selection_may_not_default_to_a_narrower_lane() -> None:
    """Ambiguous relevance must fail safe to the fuller path, not the cheaper one."""
    policy = valid_acceleration()
    policy["ci_acceleration"]["change_aware_selection"]["conservative_default"] = "fast"
    errors: list[str] = []
    policy_validator.check_ci_acceleration(policy, errors)
    assert any("conservative_default" in error for error in errors)


def test_security_sensitive_paths_may_not_be_bypassed_by_a_fast_lane() -> None:
    policy = valid_acceleration()
    policy["ci_acceleration"]["change_aware_selection"][
        "security_sensitive_path_bypass_prohibited"
    ] = False
    errors: list[str] = []
    policy_validator.check_ci_acceleration(policy, errors)
    assert any("security_sensitive_path_bypass" in error for error in errors)


def test_validation_reuse_unbound_from_its_inputs_is_rejected() -> None:
    """Reused evidence not bound to the same tree is a claim about a different tree."""
    policy = valid_acceleration()
    policy["ci_acceleration"]["validation_reuse"]["same_source_or_tree_only"] = False
    errors: list[str] = []
    policy_validator.check_ci_acceleration(policy, errors)
    assert any("same_source_or_tree_only" in error for error in errors)


def test_validation_reuse_across_a_policy_change_is_rejected() -> None:
    policy = valid_acceleration()
    policy["ci_acceleration"]["validation_reuse"][
        "source_or_relevant_policy_change_invalidates_result"
    ] = False
    errors: list[str] = []
    policy_validator.check_ci_acceleration(policy, errors)
    assert any("invalidates_result" in error for error in errors)


# ======================================================================================
# Governance manifest: development flow and validation lanes
# ======================================================================================


def valid_flow_and_lanes() -> dict[str, Any]:
    return {
        "development_flow": {
            "mixed_risk_batch_uses_highest_risk": True,
            "separate_pull_request_required_for_unrelated_objectives": True,
            (
                "separate_pull_request_required_for_independent_privileged_or_destructive_authorization_boundary"
            ): True,
            "cross_project_batching_default_allowed": False,
        },
        "ci": {
            "validation_lanes": {
                "FAST": {
                    "unknown_relevance_falls_back_to_full": True,
                    "implemented": False,
                    "not_implemented_reason": "no tested impact map exists",
                },
                "FULL": {
                    "implemented": True,
                    "required_for_security_sensitive_paths": True,
                    "required_for_dependency_or_lockfile_changes": True,
                    "required_for_ci_or_governance_changes": True,
                    "required_for_high_or_critical_risk": True,
                    "required_when_change_impact_is_ambiguous": True,
                },
            },
            "acceleration": {"full_gate_semantics_must_not_be_reduced": True},
        },
    }


def test_development_flow_and_lane_baseline_is_accepted() -> None:
    errors: list[str] = []
    governance_validator.check_development_flow(valid_flow_and_lanes(), errors)
    governance_validator.check_validation_lanes(valid_flow_and_lanes(), errors)
    assert errors == []


def test_a_manifest_with_no_development_flow_is_rejected() -> None:
    errors: list[str] = []
    governance_validator.check_development_flow({}, errors)
    assert any("development_flow is missing" in error for error in errors)


def test_a_manifest_without_a_full_lane_is_rejected() -> None:
    """A repository with no full validation lane has no acceptance gate."""
    governance = valid_flow_and_lanes()
    governance["ci"]["validation_lanes"]["FULL"]["implemented"] = False
    errors: list[str] = []
    governance_validator.check_validation_lanes(governance, errors)
    assert any("FULL.implemented" in error for error in errors)


def test_a_lane_declared_but_not_implemented_must_say_why() -> None:
    """An unimplemented lane in a manifest reads as a control that exists."""
    governance = valid_flow_and_lanes()
    del governance["ci"]["validation_lanes"]["FAST"]["not_implemented_reason"]
    errors: list[str] = []
    governance_validator.check_validation_lanes(governance, errors)
    assert any("gives no" in error for error in errors)


def test_cross_project_batching_by_default_is_rejected() -> None:
    governance = valid_flow_and_lanes()
    governance["development_flow"]["cross_project_batching_default_allowed"] = True
    errors: list[str] = []
    governance_validator.check_development_flow(governance, errors)
    assert any("cross_project_batching" in error for error in errors)


def test_validation_reuse_without_integrity_binding_is_rejected_in_the_manifest() -> None:
    governance = valid_flow_and_lanes()
    governance["ci"]["acceleration"]["same_source_validation_reuse_allowed"] = True
    errors: list[str] = []
    governance_validator.check_validation_lanes(governance, errors)
    assert any("integrity binding" in error for error in errors)
