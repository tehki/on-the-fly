"""Tests for the check that reads remote branch protection back.

`scripts/verify_branch_protection.py` is what allows the sentence *main is protected* to be
said at all (Constitution Article 2), and until it existed the comparison was performed by a
person reading two documents side by side. That reading missed two enforced settings on
2026-09-07 and got the verification date wrong in one of the two places it is recorded.

So the comparison logic is tested here against synthetic rulesets. Nothing in this file
touches the network: what is asserted is that a ruleset which does *not* match the manifest is
reported as not matching, which is the half a live read can never demonstrate.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import validate_repository_governance as governance_validator
import verify_branch_protection as protection


def declared() -> dict[str, Any]:
    """This repository's own `main_branch` block, read rather than copied."""
    manifest = governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)
    return dict(manifest["main_branch"])


def matching_ruleset() -> dict[str, Any]:
    """A ruleset built to satisfy the declaration above, and nothing more."""
    block = declared()
    return {
        "name": "main-protection",
        "id": 1,
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion", "parameters": {}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "required_linear_history", "parameters": {}},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": block["required_approvals"],
                    "require_code_owner_review": block["require_code_owner_review"],
                    "dismiss_stale_reviews_on_push": block["dismiss_stale_approvals"],
                    "required_review_thread_resolution": block["require_conversation_resolution"],
                    "allowed_merge_methods": block["allowed_merge_methods"],
                    "require_extra_approval_for_unattributed_changes": block[
                        "require_extra_approval_for_unattributed_changes"
                    ],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": block[
                        "require_up_to_date_before_merge"
                    ],
                    "required_status_checks": [
                        {"context": name} for name in block["required_status_checks"]
                    ],
                },
            },
        ],
    }


def without_rule(name: str) -> dict[str, Any]:
    ruleset = copy.deepcopy(matching_ruleset())
    ruleset["rules"] = [rule for rule in ruleset["rules"] if rule["type"] != name]
    return ruleset


def with_parameter(rule_type: str, **changes: Any) -> dict[str, Any]:
    ruleset = copy.deepcopy(matching_ruleset())
    for rule in ruleset["rules"]:
        if rule["type"] == rule_type:
            rule["parameters"].update(changes)
    return ruleset


# --------------------------------------------------------------------------------------
# What matching means
# --------------------------------------------------------------------------------------


def test_a_ruleset_built_from_the_declaration_matches_it() -> None:
    assert protection.compare(declared(), matching_ruleset()) == []


@pytest.mark.parametrize(
    "rule", ["deletion", "non_fast_forward", "required_linear_history", "pull_request"]
)
def test_a_missing_rule_is_a_mismatch(rule: str) -> None:
    """Each of these protections is expressed by a rule existing at all, so its absence is
    the whole failure — there is no parameter left to disagree with."""
    failures = protection.compare(declared(), without_rule(rule))

    assert failures, f"{rule} was removed and nothing objected"
    assert any(rule in failure for failure in failures)


def test_a_weakened_approval_count_is_a_mismatch() -> None:
    failures = protection.compare(
        declared(), with_parameter("pull_request", required_approving_review_count=99)
    )

    assert any("required_approvals" in failure for failure in failures)


def test_a_merge_method_the_manifest_does_not_allow_is_a_mismatch() -> None:
    """`allowed_merge_methods` is what keeps the linear history the manifest requires
    reachable from the merge button; adding `merge` there is how it would stop."""
    failures = protection.compare(
        declared(),
        with_parameter("pull_request", allowed_merge_methods=["squash", "rebase", "merge"]),
    )

    assert any("allowed_merge_methods" in failure for failure in failures)


def test_a_different_status_check_is_a_mismatch() -> None:
    failures = protection.compare(
        declared(),
        with_parameter(
            "required_status_checks", required_status_checks=[{"context": "something-else"}]
        ),
    )

    assert any("required_status_checks" in failure for failure in failures)


def test_a_stale_branch_may_not_be_merged_when_the_manifest_says_so() -> None:
    failures = protection.compare(
        declared(),
        with_parameter("required_status_checks", strict_required_status_checks_policy=False),
    )

    assert any("require_up_to_date_before_merge" in failure for failure in failures)


# --------------------------------------------------------------------------------------
# The ruleset itself, not its rules
# --------------------------------------------------------------------------------------


def test_a_ruleset_that_is_not_active_is_a_mismatch() -> None:
    ruleset = matching_ruleset() | {"enforcement": "evaluate"}

    assert any("enforcement" in failure for failure in protection.compare(declared(), ruleset))


def test_anyone_who_can_bypass_is_a_mismatch() -> None:
    """`bypass_actors` is deliberately empty: a rule somebody can step around is a rule that
    describes everybody else."""
    ruleset = matching_ruleset() | {"bypass_actors": [{"actor_id": 5}]}

    assert any("bypass_actors" in failure for failure in protection.compare(declared(), ruleset))


def test_a_ruleset_aimed_somewhere_else_is_a_mismatch() -> None:
    ruleset = matching_ruleset() | {
        "conditions": {"ref_name": {"include": ["refs/heads/experiment"], "exclude": []}}
    }

    assert protection.compare(declared(), ruleset)


# --------------------------------------------------------------------------------------
# What the remote enforces and the manifest never mentions
# --------------------------------------------------------------------------------------


def test_an_enforced_setting_the_manifest_never_declares_is_reported() -> None:
    """Not a failure — the remote holding more than the manifest asks is the safe direction.
    But `allowed_merge_methods` reached this repository without ever being written down, and
    a control nobody records is a control nobody maintains.
    """
    ruleset = with_parameter("pull_request", some_new_github_setting=True)

    notes = protection.unrecorded(declared(), ruleset)

    assert any("some_new_github_setting" in note for note in notes)
    assert protection.compare(declared(), ruleset) == [], "an extra setting is not a failure"


def test_a_whole_rule_the_manifest_never_declares_is_reported() -> None:
    ruleset = copy.deepcopy(matching_ruleset())
    ruleset["rules"].append({"type": "required_signatures", "parameters": {}})

    assert any("required_signatures" in note for note in protection.unrecorded(declared(), ruleset))


# --------------------------------------------------------------------------------------
# Failing to read is not passing
# --------------------------------------------------------------------------------------


def test_an_unreadable_remote_exits_two_and_says_it_proved_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure mode this exists to avoid: a check that cannot reach GitHub reporting
    success, and the sentence "main is protected" resting on it."""

    def unavailable(path: str) -> Any:
        raise protection.RemoteUnavailableError("no network")

    monkeypatch.setattr(protection, "gh_json", unavailable)

    assert protection.main([]) == 2
    captured = capsys.readouterr()
    assert "not a pass" in captured.out
    assert "UNVERIFIED" in captured.err


def test_more_than_one_ruleset_is_refused_rather_than_half_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rules from several rulesets combine. Comparing against one of them would describe less
    than what is enforced, and reporting that as a pass would be the wrong kind of true."""
    monkeypatch.setattr(
        protection, "gh_json", lambda path: [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    )

    with pytest.raises(protection.RemoteUnavailableError, match="more than one"):
        protection.active_ruleset("owner/repo")
