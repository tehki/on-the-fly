"""The refusal paths of the control that detects unauthorised pushes to main.

`scripts/verify_main_push_provenance.py` is a compensating control: it cannot stop a push
that bypassed review, it makes one loud afterwards. It is named in `security_sensitive_paths`
and CI runs it on every push to main.

**Its passing path is proven daily and its refusing paths were proven by nothing.** Every
squash-merge pushes to main, and the job prints `PASS main push provenance: <sha> extends
<sha> and carries a GitHub signature`. Nothing had ever driven it to refuse. A detective
control that only works when there is nothing to detect is not a control, and it would go on
printing PASS after the branch that catches a force push stopped working.

So each refusal is exercised here: a forced push, a deletion, history replaced rather than
extended, a commit GitHub did not sign, and an unreachable verification service — which must
fail closed, because an unverified push is precisely what this exists to surface.

The two lookups that leave the process, `git merge-base` and the commits API, are replaced.
What is under test is the decision, not the network.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import validate_repository_governance as governance_validator
import verify_main_push_provenance as provenance

BEFORE = "a" * 40
AFTER = "b" * 40
NULL = "0" * 40

SIGNED = {"verified": True, "reason": "valid"}
UNSIGNED = {"verified": False, "reason": "unsigned"}


def push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    event_name: str = "push",
    ref: str = "refs/heads/main",
    repository: str = "tehki/on-the-fly",
    **payload: Any,
) -> None:
    """Stage the environment GitHub Actions would provide for a push to main."""
    body: dict[str, Any] = {"before": BEFORE, "after": AFTER}
    body.update(payload)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(body), encoding="utf-8")

    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)


def answers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ancestor: bool = True,
    verification: dict[str, Any] | Exception = SIGNED,
) -> None:
    """Replace the two lookups that leave the process."""
    monkeypatch.setattr(provenance, "is_ancestor", lambda before, after: ancestor)

    def fetch(repository: str, sha: str) -> dict[str, Any]:
        if isinstance(verification, Exception):
            raise verification
        return verification

    monkeypatch.setattr(provenance, "fetch_commit_verification", fetch)


# ---------------------------------------------------------------------------------------
# The path CI takes on every merge, so the refusals below are refusals of something.
# ---------------------------------------------------------------------------------------


def test_a_signed_merge_that_extends_main_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    push(monkeypatch, tmp_path)
    answers(monkeypatch)

    assert provenance.main() == 0
    assert "PASS main push provenance" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------
# The refusals. Each is the whole reason the control exists.
# ---------------------------------------------------------------------------------------


def test_a_forced_push_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A history rewrite on main, which force_push_allowed: false prohibits."""
    push(monkeypatch, tmp_path, forced=True)
    answers(monkeypatch)

    assert provenance.main() == 1
    assert "force-pushed" in capsys.readouterr().err


def test_deleting_main_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    push(monkeypatch, tmp_path, deleted=True, after=NULL)
    answers(monkeypatch)

    assert provenance.main() == 1
    assert "deleted" in capsys.readouterr().err


def test_a_null_after_sha_is_a_deletion_even_without_the_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The payload flag and the sha are two statements of the same fact; either suffices."""
    push(monkeypatch, tmp_path, after=NULL)
    answers(monkeypatch)

    assert provenance.main() == 1


def test_history_replaced_rather_than_extended_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The previous head must still be reachable. This is the check a rewrite fails."""
    push(monkeypatch, tmp_path)
    answers(monkeypatch, ancestor=False)

    assert provenance.main() == 1
    assert "not an ancestor" in capsys.readouterr().err


def test_a_commit_github_did_not_sign_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merge through a pull request is signed by GitHub; a direct push is not."""
    push(monkeypatch, tmp_path)
    answers(monkeypatch, verification=UNSIGNED)

    assert provenance.main() == 1
    assert "not a GitHub-verified commit" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
        ValueError("refusing to use a non-HTTPS API base"),
        TypeError("commit response carried no verification block"),
        json.JSONDecodeError("no", "", 0),
    ],
)
def test_an_unreachable_verification_service_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    """Article 8, invariant 7. An unverified push is what this control exists to surface,
    so being unable to check must not read as nothing to report."""
    push(monkeypatch, tmp_path)
    answers(monkeypatch, verification=failure)

    assert provenance.main() == 1
    assert "could not establish provenance" in capsys.readouterr().err


@pytest.mark.parametrize("sha", ["", "abc123", "z" * 40, AFTER[:39], AFTER + "c"])
def test_a_head_that_is_not_a_full_sha_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sha: str
) -> None:
    """The shas reach `git merge-base` as arguments; they are validated before they do."""
    push(monkeypatch, tmp_path, after=sha)
    answers(monkeypatch)

    assert provenance.main() == 1


@pytest.mark.parametrize(
    "repository",
    ["", "no-slash", "owner/repo/extra", "owner/repo?ref=x", "../../etc/passwd", "owner /repo"],
)
def test_a_repository_name_that_could_reshape_the_url_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repository: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It is interpolated into an API path, so it is checked before it can be."""
    push(monkeypatch, tmp_path, repository=repository)
    answers(monkeypatch)

    assert provenance.main() == 1
    assert "not a valid owner/repo" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------
# The skips. A control that fires on the wrong event is a control people turn off.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_name", "ref"),
    [
        ("pull_request", "refs/heads/main"),
        ("push", "refs/heads/some-branch"),
        ("workflow_dispatch", "refs/heads/main"),
    ],
)
def test_anything_that_is_not_a_push_to_main_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    event_name: str,
    ref: str,
) -> None:
    push(monkeypatch, tmp_path, event_name=event_name, ref=ref)
    answers(monkeypatch)

    assert provenance.main() == 0
    assert "SKIP main push provenance" in capsys.readouterr().out


def test_creating_main_is_skipped_because_there_is_no_previous_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing to compare against, and refusing would make the first push impossible."""
    push(monkeypatch, tmp_path, created=True, before=NULL)
    answers(monkeypatch)

    assert provenance.main() == 0
    assert "branch creation" in capsys.readouterr().out


def test_a_missing_event_path_is_skipped_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Outside Actions there is no payload to read, and this runs in `make check` too."""
    push(monkeypatch, tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_PATH")

    assert provenance.main() == 0


# ---------------------------------------------------------------------------------------
# The manifest declares three requirements of this script by name.
# ---------------------------------------------------------------------------------------


def test_the_script_implements_what_the_manifest_declares_of_it() -> None:
    """`ci.main_push_provenance_require_*` name three behaviours; each has a test above.

    The manifest can declare a requirement of a script it does not read. This ties the three
    declarations to the cases that demonstrate them, so dropping one is a failing test rather
    than a quietly false line in a YAML file.
    """
    manifest = governance_validator.load_yaml(governance_validator.GOVERNANCE_FILE)
    ci = manifest["ci"]

    assert ci["main_push_provenance_reject_forced_push"] is True
    assert ci["main_push_provenance_require_previous_main_parent"] is True
    assert ci["main_push_provenance_require_github_signed_merge"] is True
    assert ci["main_push_provenance_script"] == "scripts/verify_main_push_provenance.py"
