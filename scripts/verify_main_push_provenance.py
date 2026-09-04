#!/usr/bin/env python3
"""Detect pushes to main that did not arrive through a reviewed pull request.

This is a compensating control, not a preventive one. It cannot stop an unauthorised
push; it makes one loud after the fact. It exists because remote branch protection is a
provider-side control that this repository does not yet have configured, and
REPOSITORY_GOVERNANCE_v1.2-otf1.yaml requires detection while that gap is open. Deleting this
check once branch protection is verified is a deliberate decision to record, not a
cleanup to perform silently.

Checks, in order of cost:
  1. the push was not forced (history rewrite on main);
  2. the push neither created nor deleted main;
  3. the previous head is an ancestor of the new head (no history replacement);
  4. the new head is a GitHub-created, GitHub-signed commit, which is what a merge or
     squash performed through the pull-request UI produces. A commit merged locally and
     pushed directly is unsigned by GitHub and is reported.

Security failures fail closed: if provenance cannot be established, this exits non-zero
rather than assuming the push was fine (Constitution Article 8, invariant 7).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

# Every external boundary gets an explicit timeout (handbook 36).
API_TIMEOUT_SECONDS = 15

# owner/repo as GitHub allows it. Validated before it is ever placed in a URL, so a
# hostile value cannot reshape the request path.
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

NULL_SHA = "0" * 40


def fail(message: str) -> int:
    print(f"FAIL main push provenance: {message}", file=sys.stderr)
    return 1


def load_push_event() -> dict[str, Any] | None:
    """Return the push event payload, or None when this is not a push to main."""
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        return None
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        return None

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None

    with open(event_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("push event payload was not a JSON object")
    return payload


def is_ancestor(candidate: str, descendant: str) -> bool:
    """True when candidate is reachable from descendant.

    Argument-array form with a fixed executable; no shell interpolation
    (policy input_and_execution_security.avoid_shell_interpolation).
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, descendant],
        capture_output=True,
        timeout=API_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode == 0


def fetch_commit_verification(repository: str, sha: str) -> dict[str, Any]:
    """Ask the API whether GitHub itself signed this commit.

    The URL is built only from values already validated against a strict pattern, so no
    caller-controlled text reaches the request path.
    """
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api_base.startswith("https://"):
        raise ValueError(f"refusing to use a non-HTTPS API base: {api_base!r}")

    # S310 is suppressed on this call and the urlopen below, and nowhere else. It warns
    # that a caller-supplied URL could carry a file: or custom scheme; here the scheme is
    # asserted https immediately above and the path is assembled only from values already
    # matched against strict patterns, so that risk cannot arise.
    request = urllib.request.Request(  # noqa: S310
        f"{api_base}/repos/{repository}/commits/{sha}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "on-the-fly-provenance-check",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError("commit response was not a JSON object")
    verification = payload.get("commit", {}).get("verification")
    if not isinstance(verification, dict):
        raise TypeError("commit response carried no verification block")
    return verification


def main() -> int:
    event = load_push_event()
    if event is None:
        print("SKIP main push provenance: not a push to refs/heads/main")
        return 0

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not REPOSITORY_PATTERN.match(repository):
        return fail(f"GITHUB_REPOSITORY is not a valid owner/repo value: {repository!r}")

    before = str(event.get("before", ""))
    after = str(event.get("after", ""))

    if event.get("forced") is True:
        return fail(
            "main was force-pushed. Force push is prohibited by "
            "REPOSITORY_GOVERNANCE_v1.2-otf1.yaml and rewrites history other clones depend on."
        )
    if event.get("deleted") is True or after == NULL_SHA:
        return fail("main was deleted. Branch deletion is prohibited.")
    if event.get("created") is True or before == NULL_SHA:
        print("SKIP main push provenance: branch creation has no previous head to compare")
        return 0

    for label, sha in (("before", before), ("after", after)):
        if not COMMIT_SHA_PATTERN.match(sha):
            return fail(f"push event {label} is not a full commit sha: {sha!r}")

    if not is_ancestor(before, after):
        return fail(
            f"the previous head {before[:12]} is not an ancestor of {after[:12]}. "
            "History on main was replaced rather than extended."
        )

    try:
        verification = fetch_commit_verification(repository, after)
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        # Fail closed. An unreachable verification service is an unverified push, and an
        # unverified push is exactly what this control exists to surface.
        return fail(f"could not establish provenance for {after[:12]}: {exc}")

    if verification.get("verified") is not True:
        reason = verification.get("reason", "unknown")
        return fail(
            f"commit {after[:12]} is not a GitHub-verified commit (reason: {reason}). "
            "A merge performed through a pull request is signed by GitHub; a commit pushed "
            "directly to main is not. Investigate this push, then either confirm it was an "
            "authorised emergency change recorded in docs/EXCEPTIONS.md or treat it as an "
            "unauthorised write."
        )

    print(
        f"PASS main push provenance: {after[:12]} extends {before[:12]} and carries a "
        "GitHub signature"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
