"""Tests for the policy and governance validators.

Two kinds of test here, and both are needed. The positive tests assert that the manifests
committed to this repository actually pass. The negative tests assert that the validators
refuse a weakened manifest — because a validator that cannot fail is not a gate, and a
green suite that proves nothing is exactly what handbook 64S calls theatre.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, timedelta
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
# Every source file is classified.
#
# The path check can only ask whether a declared path still exists and has an owner. The
# failure it cannot see is code moving *out* of a protected directory: the behaviour is
# unchanged, the review is gone, and nothing says so. These tests drive that from both
# ends, because a check that cannot fail is not a check.
# ---------------------------------------------------------------------------------------


def classification_manifest(**overrides: Any) -> dict[str, Any]:
    """The shape `check_source_classification` reads, with this repository's real paths."""
    section: dict[str, Any] = {
        "paths": [
            "/src/on_the_fly/domain/retention/",
            "/src/on_the_fly/domain/audio/",
            "/src/on_the_fly/infrastructure/audio/",
            "/src/on_the_fly/infrastructure/asr/",
            "/src/on_the_fly/infrastructure/translation/",
            "/src/on_the_fly/infrastructure/model_store.py",
            "/src/on_the_fly/app/",
            "/src/on_the_fly/ui/",
        ],
        "reviewed_not_sensitive": [
            "/src/on_the_fly/__init__.py",
            "/src/on_the_fly/__main__.py",
            "/src/on_the_fly/domain/__init__.py",
            "/src/on_the_fly/infrastructure/__init__.py",
            "/src/on_the_fly/domain/languages.py",
        ],
    }
    section.update(overrides)
    return {"security_sensitive_paths": section}


def test_this_repository_classifies_every_source_file() -> None:
    errors: list[str] = []
    governance_validator.check_source_classification(classification_manifest(), errors)

    assert errors == []


def test_a_file_that_left_a_protected_directory_is_caught() -> None:
    """The failure mode this check exists for, and one that actually happened.

    `model_store.py` moved out of `infrastructure/asr/` to the package root. Drop its
    explicit rule and the digest verification deciding which weights may be loaded is
    unreviewed, with every other check still green.
    """
    paths = [
        path
        for path in classification_manifest()["security_sensitive_paths"]["paths"]
        if path != "/src/on_the_fly/infrastructure/model_store.py"
    ]
    errors: list[str] = []
    governance_validator.check_source_classification(classification_manifest(paths=paths), errors)

    assert any("model_store.py" in error for error in errors)


def test_an_unclassified_file_is_refused_rather_than_assumed_safe() -> None:
    """Silence must not mean "not sensitive". A new file has to be given a home."""
    exempt = [
        path
        for path in classification_manifest()["security_sensitive_paths"]["reviewed_not_sensitive"]
        if path != "/src/on_the_fly/domain/languages.py"
    ]
    errors: list[str] = []
    governance_validator.check_source_classification(
        classification_manifest(reviewed_not_sensitive=exempt), errors
    )

    assert any("languages.py" in error and "neither" in error for error in errors)


def test_an_exemption_for_a_file_that_no_longer_exists_is_refused() -> None:
    """A stale exemption is a decision nobody is making any more."""
    errors: list[str] = []
    governance_validator.check_source_classification(
        classification_manifest(
            reviewed_not_sensitive=["/src/on_the_fly/domain/deleted_last_year.py"]
        ),
        errors,
    )

    assert any("does not exist" in error for error in errors)


def test_a_file_that_is_both_protected_and_exempt_is_refused() -> None:
    """Two answers is not an answer, and the exemption is the one that would be believed."""
    errors: list[str] = []
    governance_validator.check_source_classification(
        classification_manifest(
            reviewed_not_sensitive=[
                "/src/on_the_fly/__init__.py",
                "/src/on_the_fly/__main__.py",
                "/src/on_the_fly/domain/__init__.py",
                "/src/on_the_fly/infrastructure/__init__.py",
                "/src/on_the_fly/domain/languages.py",
                "/src/on_the_fly/app/cli.py",
            ]
        ),
        errors,
    )

    assert any("also covered by a protected path" in error for error in errors)


def test_the_desktop_composition_root_is_protected() -> None:
    """`ui/app.py` opens the microphone and wires the retention store, exactly as
    `app/cli.py` does. It was unclassified until this check was written."""
    manifest = governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)
    paths = manifest["security_sensitive_paths"]["paths"]

    assert "/src/on_the_fly/ui/" in paths


# ---------------------------------------------------------------------------------------
# Exceptions expire.
#
# Article 13: "An exception that has passed its expiry authorises nothing, whatever the code
# still does." Nothing read the date until this check existed, so the register could go on
# authorising a relaxed control for as long as nobody reread it. Every test here injects the
# day, so they assert the logic rather than the calendar.
# ---------------------------------------------------------------------------------------


EXCEPTION_TEMPLATE = """## EXC-2026-09-01-001 — A relaxed control

| Field | Value |
| --- | --- |
| **Status** | {status} |
| **Owner** | @tehki |
| **Reason** | Because. |
| **Scope** | One setting. |
| **Risk** | MODERATE. |
| **Approved by** | @tehki, 2026-09-01 |
| **Issued at** | 2026-09-01 |
| **Expires at** | {expires} |
| **Removal condition** | When it is no longer needed. |

**Compensating controls:**

1. Something else still applies.
"""


def register(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, status: str, expires: str) -> None:
    """Point the validator at a synthetic exception register."""
    path = tmp_path / "EXCEPTIONS.md"
    path.write_text(EXCEPTION_TEMPLATE.format(status=status, expires=expires), encoding="utf-8")
    monkeypatch.setattr(governance_validator, "EXCEPTIONS_FILE", path)


def citing_manifest() -> dict[str, Any]:
    return {"approval_limitation": {"exception_record": "EXC-2026-09-01-001"}}


def test_this_repositorys_exceptions_are_well_formed() -> None:
    """Every record carries Article 13's fields, a known status and an ISO expiry.

    No date comparison, so this asserts the register's shape and never rots. The clock is
    checked by the validator itself, which is what will fail on the day one expires.
    """
    records = governance_validator.parse_exception_records(
        governance_validator.EXCEPTIONS_FILE.read_text(encoding="utf-8")
    )

    assert records, "the register must contain parseable records"
    for identifier, fields in records.items():
        for field in (*governance_validator.EXCEPTION_TABLE_FIELDS, "Compensating controls"):
            assert fields.get(field), f"{identifier} is missing {field}"
        assert fields["Status"] in governance_validator.EXCEPTION_STATUSES
        date.fromisoformat(fields["Expires at"])


def test_the_live_exception_fails_the_day_after_it_expires() -> None:
    """The real record, against its own real expiry. This is the gate, not a hypothetical."""
    records = governance_validator.parse_exception_records(
        governance_validator.EXCEPTIONS_FILE.read_text(encoding="utf-8")
    )
    live = {name: f for name, f in records.items() if f["Status"] == "ACTIVE"}
    assert live, "if nothing is ACTIVE this test has nothing to protect"

    for name, fields in live.items():
        expires = date.fromisoformat(fields["Expires at"])
        errors: list[str] = []
        governance_validator.check_exception_records({}, errors, today=expires)
        assert not any(name in error for error in errors), "it is still live on its last day"

        errors = []
        governance_validator.check_exception_records({}, errors, today=expires + timedelta(days=1))
        assert any(name in error and "authorises nothing" in error for error in errors)


def test_an_expired_record_marked_expired_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The register is meant to keep the history. What it may not do is call it ACTIVE."""
    register(monkeypatch, tmp_path, status="EXPIRED", expires="2026-01-01")
    errors: list[str] = []

    governance_validator.check_exception_records({}, errors, today=date(2026, 9, 6))

    assert errors == []


def test_a_manifest_citing_a_closed_exception_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relaxed control authorised by a withdrawn exception is authorised by nothing."""
    register(monkeypatch, tmp_path, status="REMOVED", expires="2027-01-01")
    errors: list[str] = []

    governance_validator.check_exception_records(citing_manifest(), errors, today=date(2026, 9, 6))

    assert any("whose status is REMOVED" in error for error in errors)


def test_a_record_missing_an_article_13_field_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nine fields, or it is not an exception. Compensating controls are prose, not a row."""
    path = tmp_path / "EXCEPTIONS.md"
    body = EXCEPTION_TEMPLATE.format(status="ACTIVE", expires="2027-01-01")
    path.write_text(body.replace("| **Owner** | @tehki |\n", ""), encoding="utf-8")
    monkeypatch.setattr(governance_validator, "EXCEPTIONS_FILE", path)
    errors: list[str] = []

    governance_validator.check_exception_records({}, errors, today=date(2026, 9, 6))

    assert any("missing Article 13 field(s): Owner" in error for error in errors)


def test_an_expiry_that_cannot_be_read_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "Six months" is not an expiry a build can check, which is the point of requiring one."""
    register(monkeypatch, tmp_path, status="ACTIVE", expires="in six months")
    errors: list[str] = []

    governance_validator.check_exception_records({}, errors, today=date(2026, 9, 6))

    assert any("unreadable expiry" in error for error in errors)


def test_a_register_with_no_parseable_records_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty register beside a manifest that cites one is a silent loss of the record."""
    path = tmp_path / "EXCEPTIONS.md"
    path.write_text("# Exception register\n\nNothing here.\n", encoding="utf-8")
    monkeypatch.setattr(governance_validator, "EXCEPTIONS_FILE", path)
    errors: list[str] = []

    governance_validator.check_exception_records({}, errors, today=date(2026, 9, 6))

    assert any("no parseable exception records" in error for error in errors)


# ---------------------------------------------------------------------------------------
# A file this repository points a reader at is a file that is there.
# ---------------------------------------------------------------------------------------


def test_this_repository_points_only_at_files_that_exist() -> None:
    errors: list[str] = []
    governance_validator.check_referenced_paths_exist(errors)

    assert errors == []


def test_a_path_that_moved_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The real defect: a security document naming a source file three changes after it
    moved, in the row describing the control that file implements."""
    (tmp_path / "src" / "on_the_fly" / "infrastructure").mkdir(parents=True)
    (tmp_path / "src" / "on_the_fly" / "infrastructure" / "model_store.py").touch()
    (tmp_path / "docs").mkdir()
    moved_from = "infrastructure/asr/model_store.py"
    (tmp_path / "docs" / "SECURITY_PRIVACY.md").write_text(
        f"| Model weights verified | `{moved_from}` |\n", encoding="utf-8"
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "SOURCE_ROOT", tmp_path / "src")
    errors: list[str] = []

    governance_validator.check_referenced_paths_exist(errors)

    assert len(errors) == 1
    assert "docs/SECURITY_PRIVACY.md:1" in errors[0]
    assert moved_from in errors[0]


def test_both_path_shapes_are_understood(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Documents write paths from the repository root and from the package root, and a
    reader following either deserves to arrive somewhere."""
    (tmp_path / "src" / "on_the_fly" / "app").mkdir(parents=True)
    (tmp_path / "src" / "on_the_fly" / "app" / "cli.py").touch()
    (tmp_path / "scripts").mkdir()
    # Backticks are applied at runtime: this file is itself scanned, so a literal
    # backticked path that does not exist would make it fail its own subject.
    present, absent_package = "app/cli.py", "app/gone.py"
    absent_repo = "scripts/absent.py"
    (tmp_path / "notes.md").write_text(
        f"`{present}` exists, `{absent_package}` does not; `{absent_repo}` does not either.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "SOURCE_ROOT", tmp_path / "src")
    errors: list[str] = []

    governance_validator.check_referenced_paths_exist(errors)

    assert len(errors) == 2
    assert any(absent_package in error for error in errors)
    assert any(absent_repo in error for error in errors)


def test_an_illustrative_path_is_not_treated_as_a_repository_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`recording.wav` and `~/.cache/...` are examples in prose, not pointers into the tree.

    Only a backticked path beginning with one of this repository's own top-level directories
    is matched — a check that flagged every filename in a README would be turned off.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "notes.md").write_text(
        "Run `python -m on_the_fly stream recording.wav`, cached under "
        "`~/.cache/on-the-fly/models`, see `somewhere/else/wherever.py`.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "SOURCE_ROOT", tmp_path / "src")
    errors: list[str] = []

    governance_validator.check_referenced_paths_exist(errors)

    assert errors == []


# ---------------------------------------------------------------------------------------
# Every policy-stack document this repository names is one that exists.
#
# ADR 0004 makes each adoption a rename and states the consequence as settled: a missed
# rename "cannot pass silently". It could, and did — in a workflow comment, which is not a
# protected path and so failed nothing.
# ---------------------------------------------------------------------------------------


def renamed(existing: str, version: str) -> str:
    """A policy-document name that is deliberately not one of this repository's.

    Built by substitution rather than written out. `tests/` is in scope for the check, so a
    literal here would make this file the very thing it refuses — and keeping tests in scope
    is deliberate, because a genuinely stale reference in a test is still a stale reference.
    """
    return re.sub(r"v\d+\.\d+-otf\d+", version, existing)


def test_this_repository_has_no_dangling_policy_document_references() -> None:
    errors: list[str] = []
    governance_validator.check_policy_document_references(errors)

    assert errors == []


def test_a_reference_to_a_renamed_document_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real defect: a comment citing the previous manifest, long after the rename."""
    previous = renamed(governance_validator.GOVERNANCE_FILE.name, "v1.1-otf1")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        f"# The required check named by {previous}.\n", encoding="utf-8"
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    errors: list[str] = []

    governance_validator.check_policy_document_references(errors)

    assert len(errors) == 1
    assert ".github/workflows/ci.yml:1" in errors[0]
    assert previous in errors[0]


def test_a_reference_to_a_document_that_exists_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    present = renamed(governance_validator.GOVERNANCE_FILE.name, "v9.9-otf1")
    (tmp_path / present).write_text("governance:\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text(f"See {present} for the controls.\n", encoding="utf-8")
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    errors: list[str] = []

    governance_validator.check_policy_document_references(errors)

    assert errors == []


def test_a_supersedes_field_may_name_a_deleted_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR 0004 step 6 deletes the superseded document in the same pull request, so naming a
    file that is gone is exactly what `supersedes:` is for — the one correct dangling
    reference, and the check has to know it or the manifest cannot describe its own lineage.
    """
    previous = renamed(governance_validator.GOVERNANCE_FILE.name, "v1.1-otf1")
    (tmp_path / "manifest.yaml").write_text(
        f"governance:\n  supersedes: {previous}\n", encoding="utf-8"
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    errors: list[str] = []

    governance_validator.check_policy_document_references(errors)

    assert errors == []


def test_every_document_in_the_policy_stack_is_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All four stems, so renaming any one of them cannot slip through the pattern."""
    stack = [
        "CODING_AGENT_CONSTITUTION_v1.3-otf1.md",
        "CODING_AGENT_POLICY_v1.3-otf1.yaml",
        "CODING_AGENT_DEVELOPMENT_PRINCIPLES_SYSTEM_PROMPT_v1.6-otf1.md",
        governance_validator.GOVERNANCE_FILE.name,
    ]
    for name in stack:
        assert (REPO_ROOT / name).is_file(), f"{name} is the real document; keep this current"
    absent = [renamed(name, "v0.1-otf1") for name in stack]
    (tmp_path / "notes.md").write_text(
        "\n".join(f"refers to {name}" for name in absent), encoding="utf-8"
    )
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    errors: list[str] = []

    governance_validator.check_policy_document_references(errors)

    assert len(errors) == len(stack)


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


# ---------------------------------------------------------------------------------------
# Every package this project imports is a package it admitted (Article 12).
#
# `requirements.txt` says so of every entry, and promoted three packages from transitive to
# declared for the same written reason. Nothing checked the converse until these did, and
# `huggingface_hub` — which fetches every model this project loads — had slipped through it.
# ---------------------------------------------------------------------------------------


def dependency_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    module: str,
    requirements: str = "",
    where: str = "infrastructure/thing.py",
) -> None:
    """A one-file source tree and a requirements file, standing in for the repository."""
    path = tmp_path / "src" / "on_the_fly" / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(module, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(requirements, encoding="utf-8")
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "SOURCE_ROOT", tmp_path / "src")


def test_this_repository_declares_every_package_it_imports() -> None:
    errors: list[str] = []
    governance_validator.check_declared_dependencies(errors)

    assert errors == []


def test_an_undeclared_import_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The real defect, reconstructed.

    A package imported directly, present only because another package happened to ask for
    it, and named in no requirements file — so unpinned, droppable by its host, and outside
    the Article 12 review the file claims every entry passed.
    """
    dependency_tree(
        monkeypatch,
        tmp_path,
        module="def download():\n    from huggingface_hub import snapshot_download\n",
        requirements="faster-whisper==1.2.1\n",
    )
    errors: list[str] = []
    governance_validator.check_declared_dependencies(errors)

    assert any("huggingface_hub" in error and "Article 12" in error for error in errors)


@pytest.mark.parametrize(
    ("module_name", "declared"),
    [
        ("faster_whisper", "faster-whisper==1.2.1"),
        ("sherpa_onnx", "sherpa-onnx==1.13.7"),
        ("huggingface_hub", "huggingface_hub==1.30.0"),
        ("PySide6", "PySide6-Essentials==6.11.2"),
    ],
)
def test_an_import_name_that_differs_from_its_distribution_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str, declared: str
) -> None:
    """A check that cried wolf on four of eight real entries would be turned off.

    Three fold by pip's own rule — lowercase, underscores to hyphens. Only the GUI toolkit,
    whose package is named differently from its distribution, needs to be written down.
    """
    dependency_tree(
        monkeypatch,
        tmp_path,
        module=f"def go():\n    import {module_name}\n",
        requirements=f"{declared}\n",
    )
    errors: list[str] = []
    governance_validator.check_declared_dependencies(errors)

    assert errors == []


def test_the_optional_extra_counts_as_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The GUI toolkit is deliberately not in requirements.txt, and is still admitted."""
    path = tmp_path / "src" / "on_the_fly" / "ui" / "app.py"
    path.parent.mkdir(parents=True)
    path.write_text("def build():\n    from PySide6 import QtCore\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==2.5.2\n", encoding="utf-8")
    (tmp_path / "requirements-ui.txt").write_text("PySide6-Essentials==6.11.2\n", encoding="utf-8")
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "SOURCE_ROOT", tmp_path / "src")
    errors: list[str] = []
    governance_validator.check_declared_dependencies(errors)

    assert errors == []


def test_the_standard_library_and_relative_imports_are_not_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to admit: one ships with Python, the other is this project's own code."""
    dependency_tree(
        monkeypatch,
        tmp_path,
        module="import wave\nimport hashlib\n\n\ndef go():\n    from . import sibling\n",
        requirements="",
    )
    errors: list[str] = []
    governance_validator.check_declared_dependencies(errors)

    assert errors == []


def test_this_repository_imports_every_package_lazily() -> None:
    errors: list[str] = []
    governance_validator.check_third_party_imports_are_lazy(errors)

    assert errors == []


def test_a_module_level_third_party_import_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One of these ends the claim for the whole package, in every direction it buys."""
    dependency_tree(
        monkeypatch,
        tmp_path,
        module="import numpy\n\n\ndef go():\n    return numpy\n",
        requirements="numpy==2.5.2\n",
    )
    errors: list[str] = []
    governance_validator.check_third_party_imports_are_lazy(errors)

    assert any("module level" in error for error in errors)


def test_this_repositorys_domain_imports_nothing_third_party() -> None:
    errors: list[str] = []
    governance_validator.check_the_domain_imports_nothing_third_party(errors)

    assert errors == []


def test_a_third_party_import_in_the_domain_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Declared, pinned, lazy — and still wrong, because of where it is.

    The layering is what let a second translation engine land without anything above
    `infrastructure/` noticing, and it is broken by convenience rather than by intent.
    """
    dependency_tree(
        monkeypatch,
        tmp_path,
        module="def rms(frame):\n    import numpy\n    return numpy.sqrt(frame)\n",
        requirements="numpy==2.5.2\n",
        where="domain/audio/levels.py",
    )
    errors: list[str] = []
    governance_validator.check_the_domain_imports_nothing_third_party(errors)

    assert any("levels.py" in error and "numpy" in error for error in errors)


# ---------------------------------------------------------------------------------------
# `make check` runs what CI runs, in the same order.
#
# The Makefile opens by saying so, and the governance manifest lists the targets that make
# it up under `ci.required_local_targets` — a key that was read by nothing at all.
# ---------------------------------------------------------------------------------------

CI_WORKFLOW = """
jobs:
  quality:
    steps:
      - name: Install
        run: python -m pip install -r requirements.txt
      - name: Lint
        run: python -m ruff check .
      - name: Tests
        run: python -m pytest -q
"""

MAKEFILE = """PYTHON ?= python

lint:
\t$(PYTHON) -m ruff check .

test:
\t$(PYTHON) -m pytest -q

check: lint test
"""


def gate_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    makefile: str = MAKEFILE,
    workflow: str = CI_WORKFLOW,
) -> dict[str, Any]:
    """A Makefile and a workflow standing in for the repository, plus a matching manifest."""
    (tmp_path / "Makefile").write_text(makefile, encoding="utf-8")
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(workflow, encoding="utf-8")
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "MAKEFILE", tmp_path / "Makefile")
    return {
        "ci": {
            "workflow": ".github/workflows/ci.yml",
            "required_job": "quality",
            "required_local_targets": ["lint", "test"],
        }
    }


def test_this_repositorys_local_gate_mirrors_its_ci() -> None:
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(
        governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE), errors
    )

    assert errors == []


def test_the_makefile_parser_reads_this_repositorys_own_gate() -> None:
    """Without this, a parser returning nothing would make every case below vacuous."""
    targets = governance_validator.make_targets()

    assert targets["check"][0] == ["policy", "governance", "lint", "typecheck", "test"]
    assert governance_validator.local_gate_commands(targets) == [
        "python scripts/validate_coding_agent_policy.py",
        "python scripts/validate_repository_governance.py",
        "python -m ruff check .",
        "python -m ruff format --check .",
        "python -m mypy src scripts tests",
        "python -m pytest -q",
    ]


def test_a_matching_pair_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Setup steps before the gates are not a divergence: CI has to install its toolchain."""
    manifest = gate_tree(monkeypatch, tmp_path)
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert errors == []


def test_a_gate_ci_runs_and_the_makefile_does_not_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`make check` becomes a green light that does not mean anything."""
    manifest = gate_tree(
        monkeypatch,
        tmp_path,
        workflow=CI_WORKFLOW + "      - name: Secrets\n        run: python -m secret_scan\n",
    )
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("does not end with the commands" in error for error in errors)


def test_a_gate_the_makefile_runs_and_ci_does_not_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worse direction: enforced only on the machines of people who choose to run it."""
    manifest = gate_tree(
        monkeypatch,
        tmp_path,
        makefile=MAKEFILE.replace(
            "check: lint test", "audit:\n\t$(PYTHON) -m audit\n\ncheck: lint test audit"
        ),
    )
    manifest["ci"]["required_local_targets"] = ["lint", "test", "audit"]
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("does not end with the commands" in error for error in errors)


def test_the_gates_running_in_a_different_order_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "In the same order" is the Makefile's own word, and a cheap thing to keep true."""
    manifest = gate_tree(
        monkeypatch,
        tmp_path,
        makefile=MAKEFILE.replace("check: lint test", "check: test lint"),
    )
    manifest["ci"]["required_local_targets"] = ["test", "lint"]
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("does not end with the commands" in error for error in errors)


def test_a_target_left_out_of_check_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Declared as part of the gate, and not actually part of it."""
    manifest = gate_tree(
        monkeypatch, tmp_path, makefile=MAKEFILE.replace("check: lint test", "check: lint")
    )
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("nobody runs locally" in error for error in errors)


def test_a_declared_target_with_no_makefile_rule_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = gate_tree(monkeypatch, tmp_path)
    manifest["ci"]["required_local_targets"] = ["lint", "test", "audit"]
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("do not exist: audit" in error for error in errors)


def test_an_undeclared_local_gate_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty list would otherwise let every comparison below pass by having nothing."""
    manifest = gate_tree(monkeypatch, tmp_path)
    manifest["ci"]["required_local_targets"] = []
    errors: list[str] = []
    governance_validator.check_local_gate_mirrors_ci(manifest, errors)

    assert any("undeclared" in error for error in errors)


# What main_branch demands is what the recorded verification actually found.
#
# The manifest keeps the two apart on purpose — one is policy, the other is evidence read
# back from the API on a particular day — which is exactly why they can drift. Raising a
# demand is a different edit from re-verifying, and nothing compared them.
# ---------------------------------------------------------------------------------------


def protection_manifest(**overrides: Any) -> dict[str, Any]:
    """This repository's own manifest, with the recorded evidence optionally disturbed."""
    manifest = governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)
    manifest["truthfulness"]["last_verified_remote_state"].update(overrides)
    return manifest


def protection_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    governance_validator.check_declared_protection_matches_verification(manifest, errors)
    return errors


def test_this_repositorys_demands_match_what_was_verified() -> None:
    assert protection_errors(protection_manifest()) == []


def test_the_rule_table_covers_what_this_repository_actually_demands() -> None:
    """Without this, a table that had lost its entries would pass everything silently."""
    covered = {key for key, _, _ in governance_validator.PROTECTION_RULES}

    assert {"force_push_allowed", "branch_deletion_allowed", "require_linear_history"} <= covered


def test_a_demanded_rule_missing_from_the_evidence_is_caught() -> None:
    """Either main is not protected the way this file says, or the record is stale."""
    manifest = protection_manifest()
    manifest["truthfulness"]["last_verified_remote_state"]["rules_present"] = [
        rule
        for rule in manifest["truthfulness"]["last_verified_remote_state"]["rules_present"]
        if rule != "non_fast_forward"
    ]

    assert any("non_fast_forward" in error for error in protection_errors(manifest))


def test_a_required_check_that_was_never_verified_remotely_is_caught() -> None:
    """A check required locally and not remotely is not required."""
    manifest = protection_manifest(required_status_checks_verified=[])

    assert any("'quality'" in error for error in protection_errors(manifest))


def test_demanding_an_up_to_date_branch_without_the_strict_policy_is_caught() -> None:
    """Otherwise a stale branch merges on a check that never saw the current main."""
    manifest = protection_manifest(strict_required_status_checks_policy=False)

    assert any("not strict" in error for error in protection_errors(manifest))


def test_a_bypass_actor_under_the_zero_approval_exception_is_caught() -> None:
    """Zero approvals is compensated by nobody being able to bypass the rules."""
    manifest = protection_manifest(bypass_actors_count=1, current_user_can_bypass="always")

    assert any("bypass" in error for error in protection_errors(manifest))


def test_nothing_is_demanded_of_the_evidence_while_protection_is_recorded_as_absent() -> None:
    """`check_truthfulness` owns that case, and requires the compensating detection instead.

    Demanding that the evidence list the rules would be demanding evidence of something the
    same file says is not there.
    """
    manifest = protection_manifest(branch_protection_present=False, rules_present=[])

    assert protection_errors(manifest) == []


def test_a_manifest_with_no_verification_record_at_all_is_refused() -> None:
    """Article 2 forbids describing main as protected without one."""
    manifest = governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)
    manifest["truthfulness"]["last_verified_remote_state"] = {}

    errors = protection_errors(manifest)

    assert any("no record of what was ever checked" in error for error in errors)


def test_a_relaxed_demand_stops_requiring_its_rule() -> None:
    """The check follows the policy rather than insisting on a fixed set of rules.

    A repository that permitted force pushes would be wrong for other reasons, and this
    check is not the place that says so — it compares what is demanded against what was
    found, and demands nothing on behalf of a policy that has been relaxed.
    """
    manifest = protection_manifest()
    manifest["main_branch"]["force_push_allowed"] = True
    manifest["truthfulness"]["last_verified_remote_state"]["rules_present"] = [
        rule
        for rule in manifest["truthfulness"]["last_verified_remote_state"]["rules_present"]
        if rule != "non_fast_forward"
    ]

    assert protection_errors(manifest) == []


# ---------------------------------------------------------------------------------------
# Every governance check is exercised in refusal, not only in passing.
#
# `test_validator_passes_against_this_repository` runs the whole script and proves it says
# yes to a tree that is in order. Six checks had nothing else: nothing drove them to say no.
# A gate whose refusals are exercised by nothing goes on printing PASS after it stops
# working, which is what the untested provenance control was doing until it was driven to
# refuse.
# ---------------------------------------------------------------------------------------


def live_manifest() -> dict[str, Any]:
    """This repository's own manifest, to be disturbed one field at a time."""
    return governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)


def refusals(check: Any, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    check(manifest, errors)
    return errors


@pytest.mark.parametrize(
    "check_name",
    [
        "check_development_flow",
        "check_validation_lanes",
        "check_sensitive_paths",
        "check_ci_wiring",
        "check_truthfulness",
        "check_cross_document_versions",
    ],
)
def test_each_check_accepts_this_repository_as_it_stands(check_name: str) -> None:
    """The baseline every case below is a departure from."""
    assert refusals(getattr(governance_validator, check_name), live_manifest()) == []


# --- development flow (Article 15) ------------------------------------------------------


def test_a_manifest_with_no_development_flow_position_is_refused() -> None:
    manifest = live_manifest()
    del manifest["development_flow"]

    assert any(
        "stated position" in error
        for error in refusals(governance_validator.check_development_flow, manifest)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mixed_risk_batch_uses_highest_risk", False),
        ("separate_pull_request_required_for_unrelated_objectives", False),
        (
            "separate_pull_request_required_for_independent_privileged_or_destructive"
            "_authorization_boundary",
            False,
        ),
        ("cross_project_batching_default_allowed", True),
    ],
)
def test_relaxing_a_batching_rule_is_refused(field: str, value: bool) -> None:
    """Batching commits into one work-unit PR is allowed; these are what stop it becoming a
    way to slip work past review."""
    manifest = live_manifest()
    manifest["development_flow"][field] = value

    assert refusals(governance_validator.check_development_flow, manifest) != []


# --- validation lanes -------------------------------------------------------------------


def test_a_manifest_with_no_validation_lanes_is_refused() -> None:
    manifest = live_manifest()
    del manifest["ci"]["validation_lanes"]

    assert any(
        "which lanes exist" in error
        for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


def test_a_repository_without_a_full_lane_has_no_acceptance_gate() -> None:
    manifest = live_manifest()
    manifest["ci"]["validation_lanes"]["FULL"]["implemented"] = False

    assert any(
        "no acceptance gate" in error
        for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


@pytest.mark.parametrize(
    "field",
    [
        "required_for_security_sensitive_paths",
        "required_for_dependency_or_lockfile_changes",
        "required_for_ci_or_governance_changes",
        "required_for_high_or_critical_risk",
        "required_when_change_impact_is_ambiguous",
    ],
)
def test_narrowing_when_the_full_lane_is_required_is_refused(field: str) -> None:
    manifest = live_manifest()
    manifest["ci"]["validation_lanes"]["FULL"][field] = False

    assert any(
        field in error for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


@pytest.mark.parametrize("lane", ["FAST", "RELEASE"])
def test_a_lane_declared_without_being_built_or_explained_is_refused(lane: str) -> None:
    """An unimplemented lane reads as a control that exists. It says why, or it goes."""
    manifest = live_manifest()
    del manifest["ci"]["validation_lanes"][lane]["not_implemented_reason"]

    assert any(
        "reads as a control that exists" in error
        for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


def test_a_fast_lane_that_does_not_fall_back_on_unknown_relevance_is_refused() -> None:
    """The FAST lane may omit work only when it knows the work is irrelevant."""
    manifest = live_manifest()
    manifest["ci"]["validation_lanes"]["FAST"]["unknown_relevance_falls_back_to_full"] = False

    assert refusals(governance_validator.check_validation_lanes, manifest) != []


def test_reusing_validation_without_binding_it_to_the_same_inputs_is_refused() -> None:
    """Reused evidence is evidence about a tree. It has to be bound to which one."""
    manifest = live_manifest()
    manifest["ci"]["acceleration"]["same_source_validation_reuse_allowed"] = True
    manifest["ci"]["acceleration"][
        "same_source_validation_reuse_requires_input_and_artifact_integrity_binding"
    ] = False

    assert any(
        "integrity binding" in error
        for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("acceleration", "full_gate_semantics_must_not_be_reduced"),
        ("merge_queue", "full_gate_required_on_merge_group"),
    ],
)
def test_weakening_the_full_gate_for_speed_is_refused(section: str, field: str) -> None:
    """Every acceleration in this manifest is permitted on the condition that it does not
    reduce what the gate means."""
    manifest = live_manifest()
    manifest["ci"][section][field] = False

    assert any(
        field in error for error in refusals(governance_validator.check_validation_lanes, manifest)
    )


# --- sensitive paths --------------------------------------------------------------------


def test_declaring_no_sensitive_paths_at_all_is_refused() -> None:
    manifest = live_manifest()
    manifest["security_sensitive_paths"]["paths"] = []

    assert refusals(governance_validator.check_sensitive_paths, manifest) != []


def test_a_protected_path_that_does_not_exist_is_refused() -> None:
    """A protected path that is not there protects nothing."""
    manifest = live_manifest()
    manifest["security_sensitive_paths"]["paths"].append("/src/on_the_fly/nowhere.py")

    assert any(
        "does not exist" in error
        for error in refusals(governance_validator.check_sensitive_paths, manifest)
    )


def test_a_protected_directory_that_does_not_exist_yet_is_visible_as_pending() -> None:
    """A rule may precede the code it will govern, but not silently."""
    manifest = live_manifest()
    manifest["security_sensitive_paths"]["paths"].append("/src/on_the_fly/planned/")

    assert any(
        "does not exist yet" in error
        for error in refusals(governance_validator.check_sensitive_paths, manifest)
    )


def test_a_protected_path_with_no_code_owner_is_refused() -> None:
    """A path protected in the manifest and absent from CODEOWNERS is a claim with no
    mechanism behind it — which is the failure this check exists for."""
    manifest = live_manifest()
    manifest["security_sensitive_paths"]["paths"].append("/README.md")

    assert any(
        "no CODEOWNERS rule" in error
        for error in refusals(governance_validator.check_sensitive_paths, manifest)
    )


def test_declaring_sensitive_paths_with_no_codeowners_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(governance_validator, "CODEOWNERS_FILE", tmp_path / "CODEOWNERS")

    assert any(
        "CODEOWNERS does not exist" in error
        for error in refusals(governance_validator.check_sensitive_paths, live_manifest())
    )


# --- CI wiring --------------------------------------------------------------------------


def test_a_manifest_naming_no_workflow_is_refused() -> None:
    manifest = live_manifest()
    del manifest["ci"]["workflow"]

    assert any(
        "ci.workflow is missing" in error
        for error in refusals(governance_validator.check_ci_wiring, manifest)
    )


def test_a_workflow_path_that_does_not_exist_is_refused() -> None:
    manifest = live_manifest()
    manifest["ci"]["workflow"] = ".github/workflows/absent.yml"

    assert any(
        "missing file" in error
        for error in refusals(governance_validator.check_ci_wiring, manifest)
    )


def test_a_required_job_that_the_workflow_does_not_define_is_refused() -> None:
    """The remote status check is matched by job name. Rename the job and the required
    check silently stops being satisfied, which is the evasion route handbook 64M prohibits.
    """
    manifest = live_manifest()
    manifest["ci"]["required_job"] = "quality-fast"

    assert any(
        "is not defined as a job" in error
        for error in refusals(governance_validator.check_ci_wiring, manifest)
    )


@pytest.mark.parametrize(
    "field", ["policy_validator", "governance_validator", "main_push_provenance_script"]
)
def test_a_validator_script_that_is_not_there_is_refused(field: str) -> None:
    manifest = live_manifest()
    manifest["ci"][field] = "scripts/gone.py"

    assert any(
        "missing file" in error
        for error in refusals(governance_validator.check_ci_wiring, manifest)
    )


@pytest.mark.parametrize(
    "field", ["policy_validator", "governance_validator", "main_push_provenance_script"]
)
def test_a_validator_the_manifest_does_not_name_is_refused(field: str) -> None:
    manifest = live_manifest()
    del manifest["ci"][field]

    assert any(
        f"ci.{field} is missing" in error
        for error in refusals(governance_validator.check_ci_wiring, manifest)
    )


# --- truthfulness (Article 2) -----------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "never_claim_remote_branch_protection_without_verification",
        "local_ci_manifest_does_not_equal_remote_enforcement",
    ],
)
def test_dropping_a_truthfulness_invariant_is_refused(field: str) -> None:
    """These are the two lines that stop a local manifest being described as enforcement."""
    manifest = live_manifest()
    manifest["truthfulness"][field] = False

    assert any(
        field in error for error in refusals(governance_validator.check_truthfulness, manifest)
    )


def test_not_requiring_branch_protection_at_all_is_refused() -> None:
    manifest = live_manifest()
    manifest["external_control_plane"]["branch_protection_or_ruleset_required"] = False

    assert any(
        "branch_protection_or_ruleset_required" in error
        for error in refusals(governance_validator.check_truthfulness, manifest)
    )


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (
            "external_control_plane",
            "compensating_main_push_detection_required_while_unprotected",
        ),
        ("ci", "main_push_provenance_detection_required"),
    ],
)
def test_unprotected_main_without_the_compensating_detection_is_refused(
    section: str, field: str
) -> None:
    """The one state this repository must never be in quietly: no remote protection and no
    detection either."""
    manifest = live_manifest()
    manifest["truthfulness"]["last_verified_remote_state"]["branch_protection_present"] = False
    manifest[section][field] = False

    assert any(
        field in error for error in refusals(governance_validator.check_truthfulness, manifest)
    )


# --- cross-document versions (ADR 0004) -------------------------------------------------


@pytest.mark.parametrize("field", ["policy_version", "constitution_version", "handbook_version"])
def test_a_governance_version_drifting_from_the_policy_is_refused(field: str) -> None:
    """Adopting a new upstream version is a rename across the whole stack. Half a rename
    leaves two documents claiming different things are in force."""
    manifest = live_manifest()
    manifest["governance"][field] = "0.0-drifted"

    assert any(
        "version drift" in error
        for error in refusals(governance_validator.check_cross_document_versions, manifest)
    )


def test_the_governance_version_the_policy_names_must_be_this_one() -> None:
    manifest = live_manifest()
    manifest["governance"]["version"] = "0.0-drifted"

    assert any(
        "repository_governance_version" in error
        for error in refusals(governance_validator.check_cross_document_versions, manifest)
    )


# ---------------------------------------------------------------------------------------
# The policy validator refuses too.
#
# The same sweep that found six unrefused governance checks found two more here:
# check_precedence_and_versions and check_security_controls were exercised only by
# `test_validator_passes_against_this_repository`. The second is the larger, and is where
# the policy states that untrusted content is data, that deserialisation is safe, that logs
# do not carry project content, and that a required CI gate may not be reduced.
# ---------------------------------------------------------------------------------------


def live_policy() -> dict[str, Any]:
    """This repository's own policy, to be disturbed one field at a time."""
    return policy_validator.load_policy(policy_validator.POLICY_FILE)


def policy_refusals(check: Any, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    check(policy, errors)
    return errors


@pytest.mark.parametrize("check_name", ["check_precedence_and_versions", "check_security_controls"])
def test_each_policy_check_accepts_this_repository_as_it_stands(check_name: str) -> None:
    assert policy_refusals(getattr(policy_validator, check_name), live_policy()) == []


# --- precedence and versions ------------------------------------------------------------


def test_a_policy_that_is_not_deny_by_default_is_refused() -> None:
    """Article 3. A default of allow is not a policy with exceptions; it is no policy."""
    policy = live_policy()
    policy["policy"]["default_mode"] = "allow_by_default"

    assert any(
        "deny_by_default" in error
        for error in policy_refusals(policy_validator.check_precedence_and_versions, policy)
    )


@pytest.mark.parametrize("field", ["constitution_version", "handbook_version"])
def test_a_companion_version_that_names_no_document_is_refused(field: str) -> None:
    """A declared companion version must correspond to a file that is actually here.

    Adopting an upstream version is a rename (ADR 0004); a version bumped without the
    rename leaves the policy citing a document nobody can read.
    """
    policy = live_policy()
    policy["policy"][field] = "9.9-nonexistent"

    assert any(
        "not present in the repository" in error
        for error in policy_refusals(policy_validator.check_precedence_and_versions, policy)
    )


@pytest.mark.parametrize("field", ["constitution_version", "handbook_version"])
def test_a_missing_companion_version_is_refused(field: str) -> None:
    policy = live_policy()
    del policy["policy"][field]

    assert any(
        f"policy.{field} is missing" in error
        for error in policy_refusals(policy_validator.check_precedence_and_versions, policy)
    )


@pytest.mark.parametrize(
    "field",
    [
        "lower_layers_must_not_weaken_higher_layers",
        "known_material_security_defect_blocks_completion_without_authorized_exception",
    ],
)
def test_letting_a_lower_layer_weaken_the_constitution_is_refused(field: str) -> None:
    """The two lines that make the stack a stack rather than four documents."""
    policy = live_policy()
    policy["constitution_enforcement"][field] = False

    assert policy_refusals(policy_validator.check_precedence_and_versions, policy) != []


# --- security controls ------------------------------------------------------------------


def test_permitting_custom_cryptography_is_refused() -> None:
    policy = live_policy()
    policy["cryptography"]["custom_crypto_prohibited"] = False

    assert any(
        "custom_crypto_prohibited" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


@pytest.mark.parametrize(
    "field",
    [
        "untrusted_content_is_data_not_authority",
        "safe_deserialization_required",
        "protect_against_path_traversal",
        "ssrf_protection_required_for_user_controlled_destinations",
    ],
)
def test_dropping_an_input_security_control_is_refused(field: str) -> None:
    """This application reads WAV headers, model files and API responses it did not write.
    Each of these is the reason one of those is treated as hostile."""
    policy = live_policy()
    policy["input_and_execution_security"][field] = False

    assert any(
        field in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


def test_permitting_project_content_in_logs_is_refused() -> None:
    """Article 14. A log line carrying a transcript is a transcript that outlives the ten
    seconds everything else in this project is held for."""
    policy = live_policy()
    policy["observability"]["do_not_log_project_content"] = False

    assert any(
        "Article 14" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


@pytest.mark.parametrize("value", ["OPERATIONAL_METADATA", "DURABLE_PROJECT_ARTIFACT", None])
def test_a_content_bearing_log_class_other_than_ephemeral_is_refused(value: str | None) -> None:
    """If a log can carry content, it is content, and content is EPHEMERAL."""
    policy = live_policy()
    policy["observability"]["content_bearing_logs_retention_class"] = value

    assert any(
        "EPHEMERAL" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


def test_permitting_a_required_ci_gate_to_be_reduced_is_refused() -> None:
    """Handbook 64M's evasion route, stated in the policy rather than only in the workflow."""
    policy = live_policy()
    policy["optimization"]["ci_required_gate_reduction_prohibited"] = False

    assert any(
        "ci_required_gate_reduction_prohibited" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


def test_claiming_enforcement_without_verifying_the_remote_is_refused() -> None:
    """Article 2, from the policy's side. The governance manifest says the same thing, and
    both have to, because either one alone can be edited."""
    policy = live_policy()
    policy["repository_governance"]["remote_state_must_be_verified_before_claiming_enforcement"] = (
        False
    )

    assert any(
        "Article 2" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


def test_a_governance_manifest_the_policy_names_but_the_tree_lacks_is_refused() -> None:
    """The name is built rather than written out, for the reason `renamed` records: `tests/`
    is in scope for `check_policy_document_references`, so a literal here would make this
    file carry the dangling reference it is describing."""
    policy = live_policy()
    policy["repository_governance"]["manifest"] = renamed(
        governance_validator.GOVERNANCE_FILE.name, "v9.9-otf1"
    )

    assert any(
        "missing file" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


def test_a_policy_naming_no_governance_manifest_is_refused() -> None:
    policy = live_policy()
    del policy["repository_governance"]["manifest"]

    assert any(
        "manifest is missing" in error
        for error in policy_refusals(policy_validator.check_security_controls, policy)
    )


# ---------------------------------------------------------------------------------------
# `make check` runs the versions this repository declares
#
# `check_local_gate_mirrors_ci` establishes that the local gate runs the same commands as
# CI. This is the other half: the same commands can still give a different answer if they
# are different programs.
# ---------------------------------------------------------------------------------------


def toolchain_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    pins: str,
    installed: dict[str, str],
) -> list[str]:
    """Run the check against a made-up requirements file and a made-up environment."""
    (tmp_path / "requirements.txt").write_text(pins, encoding="utf-8")
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "GATE_REQUIREMENTS_FILES", ("requirements.txt",))
    monkeypatch.setattr(governance_validator, "installed_version", installed.get)
    errors: list[str] = []
    governance_validator.check_the_installed_toolchain_matches_the_pins(errors)
    return errors


def test_this_machine_is_running_the_versions_this_repository_pins() -> None:
    errors: list[str] = []
    governance_validator.check_the_installed_toolchain_matches_the_pins(errors)

    assert errors == []


def test_the_pins_are_read_and_cover_the_tools_the_gate_runs() -> None:
    """Without this, a parser that matched nothing would make the check vacuous."""
    text = "\n".join(
        (governance_validator.REPO_ROOT / name).read_text(encoding="utf-8")
        for name in governance_validator.GATE_REQUIREMENTS_FILES
    )
    pinned = {
        match.group(1)
        for line in text.splitlines()
        if (match := governance_validator.REQUIREMENT_PIN.match(line))
    }

    assert {"ruff", "mypy", "pytest"} <= pinned


def test_a_version_that_does_not_match_its_pin_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real one. The ruff pin moved to 0.16.6 on 2026-09-08, and until the next
    `pip install` this machine's `make check` ran a linter one version behind the one the
    required status check runs — which the Makefile's opening line says cannot happen.
    """
    errors = toolchain_tree(
        monkeypatch, tmp_path, pins="ruff==0.16.6\n", installed={"ruff": "0.16.5"}
    )

    assert any("pins ruff 0.16.6, but 0.16.5 is installed" in error for error in errors)


def test_a_matching_version_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert (
        toolchain_tree(monkeypatch, tmp_path, pins="ruff==0.16.6\n", installed={"ruff": "0.16.6"})
        == []
    )


def test_a_package_that_is_not_installed_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It cannot change the answer of a run that could not start, and the GUI extra is
    deliberately absent on CI (ADR 0016)."""
    assert toolchain_tree(monkeypatch, tmp_path, pins="absent==1.0\n", installed={}) == []


def test_comments_and_blank_lines_are_not_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """These files are mostly prose: every entry carries its licence and its admission."""
    pins = "# ruff==9.9.9 is not a pin\n\n  \nruff==0.16.6\n"
    errors = toolchain_tree(monkeypatch, tmp_path, pins=pins, installed={"ruff": "0.16.6"})

    assert errors == []


def test_a_missing_requirements_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate's inputs cannot be undeclared."""
    monkeypatch.setattr(governance_validator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(governance_validator, "GATE_REQUIREMENTS_FILES", ("absent.txt",))
    errors: list[str] = []
    governance_validator.check_the_installed_toolchain_matches_the_pins(errors)

    assert any("undeclared" in error for error in errors)


def test_the_optional_gui_extra_is_not_part_of_the_gate() -> None:
    """CI installs it nowhere, so a machine that has it and one that does not are both
    correct — and requiring it would fail the gate on CI itself."""
    assert "requirements-ui.txt" not in governance_validator.GATE_REQUIREMENTS_FILES
    assert "requirements-dev.txt" in governance_validator.GATE_REQUIREMENTS_FILES
