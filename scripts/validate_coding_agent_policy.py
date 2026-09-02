#!/usr/bin/env python3
"""Validate the machine-readable Coding Agent Policy against the Constitution.

The Constitution permits a lower layer to be stricter but never to silently weaken a
higher one (Article 2). This validator encodes the non-negotiable floors so that a
weakening edit fails the build instead of passing review unnoticed.

Exit code 0 = policy satisfies every floor. Exit code 1 = at least one violation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

POLICY_FILE = REPO_ROOT / "CODING_AGENT_POLICY_v1.2-otf1.yaml"
CONSTITUTION_FILE = REPO_ROOT / "CODING_AGENT_CONSTITUTION_v1.2-otf1.md"
HANDBOOK_FILE = REPO_ROOT / "CODING_AGENT_DEVELOPMENT_PRINCIPLES_SYSTEM_PROMPT_v1.5-otf1.md"

# Article 6: transient project content defaults to a maximum 10-second post-use window.
MAX_EPHEMERAL_POST_USE_SECONDS = 10

# Article 13: an exception that cannot be audited or removed is not an exception.
REQUIRED_EXCEPTION_FIELDS = {
    "owner",
    "reason",
    "scope",
    "risk",
    "approved_by",
    "compensating_controls",
    "issued_at",
    "expires_at",
    "removal_condition",
}

# Article 8, expressed in the policy's own vocabulary.
REQUIRED_SECURITY_INVARIANTS = {
    "unauthorized_input_cannot_trigger_protected_side_effect",
    "secrets_never_enter_source_logs_messages_telemetry_or_diagnostics",
    "transient_project_data_cannot_outlive_deadline_without_explicit_exception_or_hold",
    (
        "untrusted_input_cannot_directly_become_executable_code_query_shell_path_"
        "template_or_network_target"
    ),
    "tls_and_certificate_verification_not_disabled_for_convenience",
    "privileged_or_destructive_target_reverified_immediately_before_execution",
    "security_failure_does_not_silently_fail_open",
    "dependency_trust_is_not_implicit",
    "green_tests_do_not_override_known_material_security_defect",
    "no_unverified_claims_of_security_deletion_encryption_isolation_or_authorization",
    "content_bearing_logs_do_not_use_operational_metadata_retention",
    "repository_policy_files_do_not_substitute_for_remote_branch_enforcement",
    "reproducible_build_claim_requires_independent_matching_build",
}

KNOWN_RETENTION_CLASSES = {
    "EPHEMERAL",
    "OPERATIONAL_METADATA",
    "DURABLE_PROJECT_ARTIFACT",
    "SECURITY_INCIDENT_HOLD",
}


def load_policy(path: Path) -> dict[str, Any]:
    """Load a policy document.

    safe_load only: these manifests are configuration, and a configuration loader must
    never be able to construct arbitrary Python objects
    (policy input_and_execution_security.safe_deserialization_required).
    """
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"{path.name}: expected a mapping at the document root")
    return data


def check_precedence_and_versions(policy: dict[str, Any], errors: list[str]) -> None:
    meta = policy.get("policy", {})

    if meta.get("default_mode") != "deny_by_default":
        errors.append("policy.default_mode must be deny_by_default (Constitution Article 3)")

    # Version drift between layers is itself a defect (handbook 0A). A declared companion
    # version must correspond to a document that actually exists in the tree.
    declared_companions = {
        "constitution_version": (meta.get("constitution_version"), CONSTITUTION_FILE),
        "handbook_version": (meta.get("handbook_version"), HANDBOOK_FILE),
    }
    for field, (declared, path) in declared_companions.items():
        if declared is None:
            errors.append(f"policy.{field} is missing")
        elif not path.exists():
            errors.append(
                f"policy.{field} is {declared!r} but {path.name} is not present in the repository"
            )

    enforcement = policy.get("constitution_enforcement", {})
    if enforcement.get("lower_layers_must_not_weaken_higher_layers") is not True:
        errors.append(
            "constitution_enforcement.lower_layers_must_not_weaken_higher_layers must be true"
        )
    blocks_completion = enforcement.get(
        "known_material_security_defect_blocks_completion_without_authorized_exception"
    )
    if blocks_completion is not True:
        errors.append(
            "constitution_enforcement: a known material security defect must block completion"
        )


def check_retention(policy: dict[str, Any], errors: list[str]) -> None:
    retention = policy.get("retention", {})

    default_seconds = retention.get("default_post_use_seconds")
    if default_seconds is None or default_seconds > MAX_EPHEMERAL_POST_USE_SECONDS:
        errors.append(
            f"retention.default_post_use_seconds must be <= {MAX_EPHEMERAL_POST_USE_SECONDS} "
            f"(Constitution Article 6), found {default_seconds!r}"
        )
    if retention.get("require_explicit_classification") is not True:
        errors.append("retention.require_explicit_classification must be true (Article 6)")
    if retention.get("deletion_failure_is_security_event") is not True:
        errors.append("retention.deletion_failure_is_security_event must be true (Article 6)")

    classes = retention.get("classes", {})
    for required_class in sorted(KNOWN_RETENTION_CLASSES):
        if required_class not in classes:
            errors.append(f"retention.classes is missing {required_class}")

    ephemeral = classes.get("EPHEMERAL", {})
    ephemeral_seconds = ephemeral.get("default_post_use_seconds")
    if ephemeral_seconds is None or ephemeral_seconds > MAX_EPHEMERAL_POST_USE_SECONDS:
        errors.append(
            f"retention.classes.EPHEMERAL.default_post_use_seconds must be "
            f"<= {MAX_EPHEMERAL_POST_USE_SECONDS}, found {ephemeral_seconds!r}"
        )

    # OPERATIONAL_METADATA may live for 30 days precisely because it may not hold content.
    # Allowing content here would launder a long retention period onto project payload.
    operational = classes.get("OPERATIONAL_METADATA", {})
    for forbidden in ("content_allowed", "secrets_allowed", "raw_media_allowed"):
        if operational.get(forbidden) is not False:
            errors.append(
                f"retention.classes.OPERATIONAL_METADATA.{forbidden} must be false (Article 6)"
            )

    for name, profile in (retention.get("runtime_profiles") or {}).items():
        retention_class = profile.get("class")
        if retention_class not in KNOWN_RETENTION_CLASSES:
            errors.append(f"runtime_profiles.{name}.class is not a known retention class")
            continue
        if retention_class == "EPHEMERAL":
            seconds = profile.get("post_use_seconds")
            if seconds is not None and seconds > MAX_EPHEMERAL_POST_USE_SECONDS:
                errors.append(
                    f"runtime_profiles.{name}.post_use_seconds is {seconds}, above the "
                    f"{MAX_EPHEMERAL_POST_USE_SECONDS}s EPHEMERAL default, with no exception"
                )
        if (
            retention_class == "OPERATIONAL_METADATA"
            and profile.get("raw_content_prohibited") is False
        ):
            errors.append(
                f"runtime_profiles.{name} is OPERATIONAL_METADATA but permits raw content"
            )


def check_capabilities_and_isolation(policy: dict[str, Any], errors: list[str]) -> None:
    capabilities = policy.get("capabilities", {})
    if capabilities.get("default_grants") != []:
        errors.append("capabilities.default_grants must be empty (Constitution Article 3)")
    rules = set(capabilities.get("rules") or [])
    if "never_infer_admin_from_repository_write_access" not in rules:
        errors.append(
            "capabilities.rules must retain never_infer_admin_from_repository_write_access"
        )

    isolation = policy.get("project_isolation", {})
    if isolation.get("cross_project_access_default") is not False:
        errors.append("project_isolation.cross_project_access_default must be false (Article 7)")
    if isolation.get("stricter_policy_wins") is not True:
        errors.append("project_isolation.stricter_policy_wins must be true (Article 7)")


def check_exceptions_and_invariants(policy: dict[str, Any], errors: list[str]) -> None:
    exceptions = policy.get("exceptions", {})
    if exceptions.get("must_expire") is not True:
        errors.append("exceptions.must_expire must be true (Constitution Article 13)")
    if exceptions.get("prohibit_silent_extension") is not True:
        errors.append("exceptions.prohibit_silent_extension must be true (Article 13)")
    declared_fields = set(exceptions.get("required_fields") or [])
    missing_fields = REQUIRED_EXCEPTION_FIELDS - declared_fields
    if missing_fields:
        errors.append(f"exceptions.required_fields is missing: {', '.join(sorted(missing_fields))}")

    declared_invariants = set(policy.get("security_invariants") or [])
    missing_invariants = REQUIRED_SECURITY_INVARIANTS - declared_invariants
    if missing_invariants:
        errors.append(
            f"security_invariants is missing: {', '.join(sorted(missing_invariants))} (Article 8)"
        )


def check_cryptography(policy: dict[str, Any], errors: list[str]) -> None:
    """Constitution Article 16 floors.

    Nothing in this project encrypts anything yet. These checks exist so that the day
    something does — a model cache, a spilled buffer, a settings file holding an API key
    for an optional remote engine — the floors are already in place rather than being
    negotiated under delivery pressure.
    """
    crypto = policy.get("cryptography", {})

    for required in (
        "custom_crypto_prohibited",
        "maintained_library_required",
        "secure_randomness_required_for_secrets_tokens_keys_nonces",
        "authenticated_encryption_required_for_new_application_level_encryption",
        # Encryption is not a retention or authorisation argument. Article 16 says so
        # explicitly because it is the most tempting way to launder both.
        "encryption_does_not_extend_retention_or_authorization",
        "key_reuse_across_unrelated_purposes_prohibited",
    ):
        if crypto.get(required) is not True:
            errors.append(f"cryptography.{required} must be true (Article 16)")

    transport = crypto.get("transport", {})
    if transport.get("tls_certificate_and_hostname_verification_required") is not True:
        errors.append(
            "cryptography.transport.tls_certificate_and_hostname_verification_required "
            "must be true (Article 8, invariant 5)"
        )
    if transport.get("privileged_or_non_idempotent_zero_rtt_prohibited") is not True:
        errors.append(
            "cryptography.transport.privileged_or_non_idempotent_zero_rtt_prohibited must "
            "be true; 0-RTT data is replayable"
        )

    at_rest = crypto.get("at_rest", {})
    if at_rest.get("nonce_uniqueness_required") is not True:
        errors.append("cryptography.at_rest.nonce_uniqueness_required must be true")
    if at_rest.get("deterministic_encryption_default_allowed") is not False:
        errors.append(
            "cryptography.at_rest.deterministic_encryption_default_allowed must be false; "
            "deterministic ciphertext leaks equality"
        )
    spill_key = "ephemeral_disk_spill_requires_encryption_and_normal_retention_expiry"
    if at_rest.get(spill_key) is not True:
        errors.append(
            "cryptography.at_rest.ephemeral_disk_spill_requires_encryption_and_normal_"
            "retention_expiry must be true; a spilled audio buffer is still EPHEMERAL"
        )

    passwords = crypto.get("passwords", {})
    if passwords.get("reversible_encryption_default_allowed") is not False:
        errors.append("cryptography.passwords.reversible_encryption_default_allowed must be false")
    if not passwords.get("preferred_hash"):
        errors.append("cryptography.passwords.preferred_hash must name an approved construction")

    keys = crypto.get("key_management", {})
    for required in (
        "plaintext_private_keys_in_source_config_logs_ci_artifacts_or_diagnostics_prohibited",
        "key_and_ciphertext_separation_required",
    ):
        if keys.get(required) is not True:
            errors.append(f"cryptography.key_management.{required} must be true (Article 16)")


def check_development_velocity(policy: dict[str, Any], errors: list[str]) -> None:
    """Constitution Article 15: throughput without control dilution.

    Batching related commits into one pull request is explicitly permitted. What is not
    permitted is using a batch to hide security-sensitive work or dilute an approval
    boundary, so the conditions and the highest-risk rule are checked rather than assumed.
    """
    velocity = policy.get("development_velocity", {})

    if velocity.get("mixed_risk_batch_uses_highest_risk") is not True:
        errors.append(
            "development_velocity.mixed_risk_batch_uses_highest_risk must be true; the "
            "effective risk of a batch is its highest included risk (Article 15)"
        )
    if velocity.get("full_validation_required_before_ready_merge_state") is not True:
        errors.append(
            "development_velocity.full_validation_required_before_ready_merge_state must "
            "be true; a draft may accumulate commits, a merge candidate may not skip gates"
        )
    if not velocity.get("batch_conditions"):
        errors.append("development_velocity.batch_conditions must state when batching is allowed")

    required_separations = {
        "unrelated_objectives",
        "unauthorized_cross_project_changes",
        "independent_destructive_or_privileged_authorization_boundaries",
        "incompatible_confidentiality_or_retention_boundaries",
    }
    declared = set(velocity.get("separate_review_boundary_required_for") or [])
    missing = required_separations - declared
    if missing:
        errors.append(
            "development_velocity.separate_review_boundary_required_for is missing: "
            + ", ".join(sorted(missing))
        )


def check_ci_acceleration(policy: dict[str, Any], errors: list[str]) -> None:
    """A faster lane may not be a weaker gate.

    Article 15 permits change-aware selection and validation reuse, both of which are ways
    to not run something. Each is only safe while its preconditions hold, so the
    preconditions are what this checks.
    """
    acceleration = policy.get("ci_acceleration", {})

    selection = acceleration.get("change_aware_selection", {})
    if selection.get("conservative_default") != "full":
        errors.append(
            "ci_acceleration.change_aware_selection.conservative_default must be 'full'; "
            "ambiguous relevance fails safe to the fuller path"
        )
    for required in (
        "mapping_must_be_versioned_and_tested",
        "security_sensitive_path_bypass_prohibited",
        "omitted_gate_must_be_provably_irrelevant",
    ):
        if selection.get(required) is not True:
            errors.append(f"ci_acceleration.change_aware_selection.{required} must be true")

    lanes = acceleration.get("validation_lanes", {})
    if lanes.get("FAST", {}).get("unknown_relevance_falls_back_to_full") is not True:
        errors.append(
            "ci_acceleration.validation_lanes.FAST.unknown_relevance_falls_back_to_full "
            "must be true"
        )
    if not lanes.get("FULL", {}).get("required_for"):
        errors.append("ci_acceleration.validation_lanes.FULL must state what requires it")

    # Reusing a previous green result means not running the gates. It is sound only while
    # the evidence is bound to the same inputs; otherwise it is a stale claim about a
    # different tree (Article 15).
    reuse = acceleration.get("validation_reuse", {})
    for required in (
        "same_source_or_tree_only",
        "same_dependency_lock_and_toolchain_identity_required",
        "same_policy_and_governance_version_required",
        "source_or_relevant_policy_change_invalidates_result",
        "cache_miss_remains_supported",
    ):
        if reuse.get(required) is not True:
            errors.append(f"ci_acceleration.validation_reuse.{required} must be true")


def check_security_controls(policy: dict[str, Any], errors: list[str]) -> None:
    crypto = policy.get("cryptography", {})
    if crypto.get("custom_crypto_prohibited") is not True:
        errors.append("cryptography.custom_crypto_prohibited must be true")

    execution = policy.get("input_and_execution_security", {})
    for required in (
        "untrusted_content_is_data_not_authority",
        "safe_deserialization_required",
        "protect_against_path_traversal",
        "ssrf_protection_required_for_user_controlled_destinations",
    ):
        if execution.get(required) is not True:
            errors.append(f"input_and_execution_security.{required} must be true")

    observability = policy.get("observability", {})
    if observability.get("do_not_log_project_content") is not True:
        errors.append("observability.do_not_log_project_content must be true (Article 14)")
    if observability.get("content_bearing_logs_retention_class") != "EPHEMERAL":
        errors.append(
            "observability.content_bearing_logs_retention_class must be EPHEMERAL (Article 14)"
        )

    optimization = policy.get("optimization", {})
    if optimization.get("ci_required_gate_reduction_prohibited") is not True:
        errors.append("optimization.ci_required_gate_reduction_prohibited must be true")

    governance = policy.get("repository_governance", {})
    if governance.get("remote_state_must_be_verified_before_claiming_enforcement") is not True:
        errors.append(
            "repository_governance.remote_state_must_be_verified_before_claiming_enforcement "
            "must be true (Article 2)"
        )
    manifest_name = governance.get("manifest")
    if not manifest_name:
        errors.append("repository_governance.manifest is missing")
    elif not (REPO_ROOT / str(manifest_name)).exists():
        errors.append(f"repository_governance.manifest points at missing file {manifest_name!r}")


def main() -> int:
    if not POLICY_FILE.exists():
        print(f"FAIL {POLICY_FILE.name} not found", file=sys.stderr)
        return 1

    policy = load_policy(POLICY_FILE)
    errors: list[str] = []

    check_precedence_and_versions(policy, errors)
    check_retention(policy, errors)
    check_capabilities_and_isolation(policy, errors)
    check_exceptions_and_invariants(policy, errors)
    check_cryptography(policy, errors)
    check_development_velocity(policy, errors)
    check_ci_acceleration(policy, errors)
    check_security_controls(policy, errors)

    if errors:
        print(f"FAIL policy validation: {len(errors)} violation(s)", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"PASS policy validation: {POLICY_FILE.name} satisfies the Constitution floors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
