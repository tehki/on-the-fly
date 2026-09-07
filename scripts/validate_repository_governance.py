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

import ast
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

GOVERNANCE_FILE = REPO_ROOT / "REPOSITORY_GOVERNANCE_v1.2-otf1.yaml"
POLICY_FILE = REPO_ROOT / "CODING_AGENT_POLICY_v1.3-otf1.yaml"
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


# Article 13's nine fields, as they are written in docs/EXCEPTIONS.md. Compensating controls
# are prose rather than a table row, so they are checked separately.
EXCEPTION_TABLE_FIELDS = (
    "Status",
    "Owner",
    "Reason",
    "Scope",
    "Risk",
    "Approved by",
    "Issued at",
    "Expires at",
    "Removal condition",
)
EXCEPTION_STATUSES = ("ACTIVE", "EXPIRED", "REMOVED")


def parse_exception_records(text: str) -> dict[str, dict[str, str]]:
    """Read `docs/EXCEPTIONS.md` into `{identifier: {field: value}}`.

    Each record is a `## EXC-...` heading followed by a `| **Field** | Value |` table. The
    parser is deliberately literal: a record whose fields it cannot read is a record whose
    expiry nobody can check, and that must surface as a violation rather than be skipped.
    """
    records: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(EXC-\d{4}-\d{2}-\d{2}-\d+)", line.strip())
        if heading:
            current = heading.group(1)
            records[current] = {}
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None:
            continue
        row = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.*?)\s*\|$", line.strip())
        if row:
            records[current][row.group(1).strip()] = row.group(2).strip()
        elif "**Compensating controls**" in line or "**Compensating controls:**" in line:
            records[current]["Compensating controls"] = "present"
    return records


def check_exception_records(
    governance: dict[str, Any], errors: list[str], today: date | None = None
) -> None:
    """Exceptions carry Article 13's fields, and none has outlived its expiry.

    `check_branch_rules` already refuses a zero-approval requirement that cites no exception,
    and refuses one whose identifier is absent from the register. What it cannot see is the
    thing Article 13 is actually about: *"An exception that has passed its expiry authorises
    nothing, whatever the code still does."* Nothing read the date, so the register could go
    on authorising a relaxed control for as long as nobody reread it.

    So the expiry is enforced against the clock, and a cited record must additionally still
    be ACTIVE — a governance manifest pointing at a REMOVED exception is authorised by
    nothing at all.
    """
    if not EXCEPTIONS_FILE.exists():
        return

    now = today or date.today()
    records = parse_exception_records(EXCEPTIONS_FILE.read_text(encoding="utf-8"))
    if not records:
        errors.append(f"docs/{EXCEPTIONS_FILE.name} contains no parseable exception records")
        return

    for identifier, fields in sorted(records.items()):
        missing = [
            field
            for field in (*EXCEPTION_TABLE_FIELDS, "Compensating controls")
            if not fields.get(field)
        ]
        if missing:
            errors.append(f"{identifier} is missing Article 13 field(s): {', '.join(missing)}")

        status = fields.get("Status", "")
        if status and status not in EXCEPTION_STATUSES:
            errors.append(
                f"{identifier} has status {status!r}; expected one of "
                f"{', '.join(EXCEPTION_STATUSES)}"
            )

        raw_expiry = fields.get("Expires at", "")
        if not raw_expiry:
            continue
        try:
            expires = date.fromisoformat(raw_expiry)
        except ValueError:
            errors.append(
                f"{identifier} has an unreadable expiry {raw_expiry!r}; it must be an "
                "ISO date (YYYY-MM-DD) so that it can be checked against the clock"
            )
            continue

        if status == "ACTIVE" and expires < now:
            errors.append(
                f"{identifier} is ACTIVE but expired on {expires.isoformat()}. An exception "
                "past its expiry authorises nothing (Article 13): either renew it "
                "deliberately with a new expiry, or remove the behaviour it covers and mark "
                "it EXPIRED."
            )

    cited = (governance.get("approval_limitation") or {}).get("exception_record")
    if cited and cited in records:
        status = records[cited].get("Status", "")
        if status and status != "ACTIVE":
            errors.append(
                f"approval_limitation cites {cited}, whose status is {status}. A relaxed "
                "control must be authorised by a live exception, not a closed one."
            )


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


SOURCE_ROOT = REPO_ROOT / "src"


def classification_of(relative: str, sensitive: list[str]) -> bool:
    """Whether a repository-relative path is covered by any declared sensitive path."""
    for declared in sensitive:
        target = str(declared).lstrip("/")
        if target.endswith("/"):
            if relative.startswith(target):
                return True
        elif relative == target:
            return True
    return False


def check_source_classification(governance: dict[str, Any], errors: list[str]) -> None:
    """Every source file is protected, or is declared in writing not to need protecting.

    `check_sensitive_paths` asks whether each declared path exists and has an owner. It
    cannot see the opposite failure, which is the one that happens by accident: code that
    moves *out* of a protected directory keeps every line of its behaviour and silently
    loses its review, and nothing in this repository would have said so.

    That is not hypothetical. `model_store.py` — which decides which model weights may be
    loaded — moved from `infrastructure/asr/` to the package root, and `/src/on_the_fly/
    infrastructure/` is not itself a declared path. Writing this check also found that
    `ui/app.py` had never been covered at all, despite opening the microphone and wiring
    the retention store, which is exactly what `/src/on_the_fly/app/` is protected for.

    So the rule is that no source file may be unclassified. Adding one costs a line in the
    manifest and a decision about which list it belongs in, which is the right amount of
    friction for adding code to an application arranged around a retention promise.
    """
    section = governance.get("security_sensitive_paths", {})
    sensitive = [str(path) for path in (section.get("paths") or [])]
    exempt_declared = [str(path) for path in (section.get("reviewed_not_sensitive") or [])]
    exempt = {path.lstrip("/") for path in exempt_declared}

    if not SOURCE_ROOT.is_dir():
        errors.append("security_sensitive_paths: src/ does not exist")
        return

    for source in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        relative = source.relative_to(REPO_ROOT).as_posix()
        if classification_of(relative, sensitive) or relative in exempt:
            continue
        errors.append(
            f"security_sensitive_paths: /{relative} is in neither paths nor "
            "reviewed_not_sensitive. Every source file must be classified: protect it, or "
            "record in writing that it does not need protecting."
        )

    for declared in exempt_declared:
        relative = declared.lstrip("/")
        if not (REPO_ROOT / relative).exists():
            errors.append(
                f"reviewed_not_sensitive: {declared} does not exist. A stale exemption is a "
                "decision nobody is making any more."
            )
        elif classification_of(relative, sensitive):
            errors.append(
                f"reviewed_not_sensitive: {declared} is also covered by a protected path. "
                "One of the two is wrong, and leaving both leaves the file's status unclear."
            )


# Where a reference to a file could plausibly live. Binary and vendored trees are skipped
# rather than decoded; nothing outside these carries prose about this repository's layout.
REFERENCE_SUFFIXES = (".md", ".yaml", ".yml", ".py", ".toml", ".txt", ".cfg")
REFERENCE_EXTRA_FILES = (".github/CODEOWNERS", "Makefile")
REFERENCE_SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def referencing_files() -> list[Path]:
    """Every text file in the repository that could name another file in it."""
    found = [
        path
        for path in sorted(REPO_ROOT.rglob("*"))
        if path.is_file()
        and path.suffix in REFERENCE_SUFFIXES
        and not REFERENCE_SKIP_DIRS.intersection(path.parts)
    ]
    found.extend(REPO_ROOT / name for name in REFERENCE_EXTRA_FILES if (REPO_ROOT / name).is_file())
    return found


# A path this repository names in prose. Two shapes, because the documents use both: from the
# repository root, and from the package root the way the source files refer to each other.
REPO_PATH_REFERENCE = re.compile(r"`((?:src|scripts|docs|tests)/[A-Za-z0-9_./-]+)`")
PACKAGE_PATH_REFERENCE = re.compile(r"`((?:app|domain|infrastructure|ui)/[A-Za-z0-9_./-]+)`")


def check_referenced_paths_exist(errors: list[str]) -> None:
    """A file this repository points a reader at must be a file that is there.

    It was written after `docs/SECURITY_PRIVACY.md` was found pointing at the old location of
    `infrastructure/model_store.py`, in the row describing how model weights are verified,
    three changes after that file moved to the package root. Nothing failed, because a path in
    a table cell is not a protected path, and a reader following the pointer would have found
    nothing at all. (The stale path is described rather than written out, because writing it
    out would make this docstring the very thing the check refuses.)

    Only unambiguous repository paths are matched: a backticked path beginning with one of
    this repository's own top-level directories. Illustrative paths like `recording.wav` are
    not, and are not meant to be.
    """
    package_root = SOURCE_ROOT / "on_the_fly"
    for path in referencing_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative_name = path.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for reference, base in (
                (REPO_PATH_REFERENCE, REPO_ROOT),
                (PACKAGE_PATH_REFERENCE, package_root),
            ):
                for named in reference.findall(line):
                    if not (base / named.rstrip("/")).exists():
                        errors.append(
                            f"{relative_name}:{number} points at {named}, which does not "
                            "exist. Update the reference or remove it; a pointer to a file "
                            "that moved is worse than none, because it reads as current."
                        )


# The requirements files, and the one package whose import name is not its distribution
# name. `PySide6-Essentials` ships the `PySide6` package; every other entry normalises by
# lowercasing and folding underscores to hyphens, which is what pip itself does.
REQUIREMENTS_FILES = ("requirements.txt", "requirements-ui.txt")
DISTRIBUTION_FOR_IMPORT = {"pyside6": "pyside6-essentials"}

# Requirement lines are `name==version`; markers and extras are not used in this repository
# and would need handling here if they ever were.
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==")


def normalise_distribution(name: str) -> str:
    """Fold a distribution or import name the way pip compares them."""
    folded = name.strip().lower().replace("_", "-")
    return DISTRIBUTION_FOR_IMPORT.get(folded, folded)


def declared_dependencies() -> set[str]:
    """Every distribution this repository declares, from all its requirements files."""
    declared: set[str] = set()
    for filename in REQUIREMENTS_FILES:
        path = REPO_ROOT / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = REQUIREMENT_NAME.match(line)
            if match is not None:
                declared.add(normalise_distribution(match.group(1)))
    return declared


def third_party_imports() -> list[tuple[Path, str, int, bool]]:
    """Every non-stdlib, non-first-party import under `src/`.

    Yields `(file, root module, line, at module level)`. The root module is what decides
    which distribution supplies it, and whether the import sits at module level is what
    decides whether it is paid for on start-up.
    """
    found: list[tuple[Path, str, int, bool]] = []
    package_root = SOURCE_ROOT / "on_the_fly"
    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - the linters catch these first
            continue
        module_level = {
            node.lineno for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import has no module of its own to admit.
                roots = [] if node.level else [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if not root or root == "on_the_fly" or root in sys.stdlib_module_names:
                    continue
                found.append((path, root, node.lineno, node.lineno in module_level))
    return found


def check_declared_dependencies(errors: list[str]) -> None:
    """Every package this project imports is a package it admitted (Article 12).

    `requirements.txt` opens by saying that every entry passed an Article 12 admission
    review, and three of its entries were promoted from transitive to declared for the same
    stated reason: a package that arrives under another package is that package's choice,
    and this project now imports it directly. Nothing checked the converse — that everything
    imported directly is declared at all — and one package had slipped through it.

    `huggingface_hub` fetches every model this project loads. It was imported directly by
    the model store, declared in no requirements file, and present only because
    `faster-whisper` asks for `huggingface-hub>=0.21`. So the most trust-sensitive network
    call in the codebase ran at whatever version that range resolved to, in a file whose
    header says versions are pinned for reproducibility, and the error it raises when the
    import fails told the reader to install requirements that never contained it.

    Import roots are matched against distribution names the way pip compares them. That
    covers `faster_whisper`, `sherpa_onnx` and `huggingface_hub` without a table; only
    `PySide6-Essentials`, whose package is named differently from its distribution, needs
    an entry.
    """
    declared = declared_dependencies()
    for path, root, line, _ in third_party_imports():
        if normalise_distribution(root) not in declared:
            relative_name = path.relative_to(REPO_ROOT).as_posix()
            errors.append(
                f"{relative_name}:{line} imports {root!r}, which no requirements file "
                "declares. A package this project imports directly is a package it depends "
                "on, whoever else happens to install it; admit it under Article 12, pin it, "
                "and record the review."
            )


def check_third_party_imports_are_lazy(errors: list[str]) -> None:
    """No third-party package is imported at module level.

    Every entry in `requirements.txt` claims its package is imported "lazily", and the
    claim carries weight in three directions: the domain and its tests run without any of
    the heavy engines installed, `--help` does not pay to load ONNX Runtime, and an optional
    extra like the GUI toolkit stays genuinely optional. All of that is a property of where
    the `import` statement sits, and a module-level import in one file would quietly end it
    for the whole package.
    """
    for path, root, line, at_module_level in third_party_imports():
        if at_module_level:
            relative_name = path.relative_to(REPO_ROOT).as_posix()
            errors.append(
                f"{relative_name}:{line} imports {root!r} at module level. Every "
                "requirements.txt entry claims its package is imported lazily; move it "
                "inside the function that needs it, so importing this module does not."
            )


def check_the_domain_imports_nothing_third_party(errors: list[str]) -> None:
    """`domain/` depends on no package at all — the layering ADR 0002 is built on.

    `requirements.txt` says of each engine that it is imported "never in domain/", and the
    port that let a whole second translation engine land without touching anything above
    `infrastructure/` (ADR 0018) only works while that stays true. It is the kind of rule
    that is never broken deliberately and is broken easily: one convenience import of numpy
    in a domain module, and the layer that is supposed to be pure Python is not.
    """
    domain_root = SOURCE_ROOT / "on_the_fly" / "domain"
    for path, root, line, _ in third_party_imports():
        if path.is_relative_to(domain_root):
            relative_name = path.relative_to(REPO_ROOT).as_posix()
            errors.append(
                f"{relative_name}:{line} imports {root!r}. The domain layer imports no "
                "third-party package: it is what lets the engines be swapped, and the "
                "tests run, without it noticing."
            )


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
    check_source_classification(governance, errors)
    check_exception_records(governance, errors)
    check_referenced_paths_exist(errors)
    check_declared_dependencies(errors)
    check_third_party_imports_are_lazy(errors)
    check_the_domain_imports_nothing_third_party(errors)
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
