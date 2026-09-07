"""The type checker's exemptions describe packages truthfully.

Each `ignore_missing_imports` override in `pyproject.toml` carries a comment saying the
package ships no `py.typed` marker, and how narrow the untyped surface therefore is. That
is a claim about somebody else's wheel, made at the moment the override was written and
never asked again — so it goes stale silently, as a package starts shipping types or as one
is added to the list that never lacked them.

It had. `huggingface_hub` was listed as untyped and ships `py.typed` at the version ADR 0036
admitted, which meant the exemption suppressed nothing and described the package wrongly, in
the block covering the code that fetches model weights.

An inert exemption is not dangerous on its own. It is dangerous as a habit: a list of
packages nobody re-reads is where a real exemption goes to hide, and `warn_unused_configs`
does not cover this case — an `ignore_missing_imports` section for a module mypy *can*
resolve is still consulted, so mypy has nothing to warn about.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Exempt for a different, still-true reason: PySide6 ships py.typed, and its override exists
# because CI deliberately does not install the 76 MB wheel (ADR 0016), so the type checker
# must pass both with the package and without it. That is a claim about this repository's CI
# rather than about the package, and it is not what this module checks.
EXEMPT_FOR_INSTALLABILITY = {"PySide6"}


def ignore_missing_imports_modules() -> list[str]:
    """Every module named by an `ignore_missing_imports` override, wildcards resolved away."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = config["tool"]["mypy"].get("overrides", [])
    modules: list[str] = []
    for override in overrides:
        if not override.get("ignore_missing_imports"):
            continue
        declared = override["module"]
        names = [declared] if isinstance(declared, str) else declared
        for name in names:
            # `PySide6.*` covers submodules of a package already named on its own; the
            # top-level name is what decides whether the distribution ships a marker.
            root = name.split(".")[0]
            if root not in modules:
                modules.append(root)
    return modules


def ships_py_typed(module: str) -> bool | None:
    """True, False, or None when the package is not installed here."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # pragma: no cover - depends on what is installed
        return None
    if spec is None or spec.origin is None:
        return None
    return (Path(spec.origin).parent / "py.typed").exists()


def test_the_overrides_are_read_and_not_empty() -> None:
    """Without this, a parser returning nothing would make the check below vacuous."""
    modules = ignore_missing_imports_modules()

    assert "sounddevice" in modules
    assert len(modules) >= 5


@pytest.mark.parametrize("module", ignore_missing_imports_modules())
def test_an_exempted_package_really_ships_no_types(module: str) -> None:
    """The claim each override's comment makes, asked of the installed package.

    Skipped rather than failed when the package is absent: an override for something not
    installed is doing exactly its job, and CI installs neither the GUI extra nor anything
    else this repository chose to leave out.
    """
    if module in EXEMPT_FOR_INSTALLABILITY:
        pytest.skip(f"{module} is exempted for installability, not for missing types")

    typed = ships_py_typed(module)
    if typed is None:
        pytest.skip(f"{module} is not installed here, which is what the override is for")

    assert not typed, (
        f"{module} ships py.typed, so the ignore_missing_imports override for it "
        "suppresses nothing and says something untrue about the package. Drop it from "
        "pyproject.toml, or record why it is still needed."
    )


def test_first_party_code_is_never_exempted_from_import_checking() -> None:
    """These exemptions are for other people's packages. This project's own are checked."""
    assert "on_the_fly" not in ignore_missing_imports_modules()
