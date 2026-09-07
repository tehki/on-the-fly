"""The tool that produces the pins everything else refuses to load without.

`ModelStore` will not load a model that has no pin, and a pin is the SHA-256 of every file
the model needs. `scripts/pin_model.py` is what produces one, and the error messages that
send a maintainer there — "Add one with scripts/pin_model.py and commit the pin" — make it
part of the trust story rather than a convenience. It had no tests.

What is checked here is the part specific to this script: that it stages the download
somewhere it will not fill memory, that it digests exactly the files asked for, and that the
block it prints is one a reviewer can paste into the pin registry and have mean what it says.
The digesting itself is `compute_digests`, which `tests/test_asr.py` already covers.

The network is replaced. `snapshot_download` is the boundary; what is under test is
everything this script does around it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pin_model
import pytest

FILES = ("config.json", "model.bin")
REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the hub with something that writes known bytes and records the call."""
    calls: list[dict[str, Any]] = []

    def snapshot_download(**kwargs: Any) -> str:
        calls.append(kwargs)
        target = Path(kwargs["local_dir"])
        target.mkdir(parents=True, exist_ok=True)
        for name in kwargs["allow_patterns"]:
            (target / name).write_bytes(name.encode())
        return str(target)

    module = type(pin_model)("huggingface_hub")
    module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", module)
    return calls


def run(tmp_path: Path, *extra: str) -> int:
    argv = ["owner/some-model", "--revision", REVISION, "--work-dir", str(tmp_path / "scratch")]
    for name in FILES:
        argv += ["--file", name]
    return pin_model.main([*argv, *extra])


# ---------------------------------------------------------------------------------------
# Where the download is staged.
# ---------------------------------------------------------------------------------------


def test_the_download_is_staged_under_the_work_dir_and_not_in_tmp(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """`/tmp` is a tmpfs on most current Linux systems, so staging a model there spends RAM.

    The models pinned so far are all under 120 MB. A Whisper `small` is 484 MB and a
    `medium` is 1.5 GB, and the first person to pin one should not discover this.
    """
    assert run(tmp_path) == 0
    capsys.readouterr()

    staged = Path(hub[0]["local_dir"])
    assert staged.is_relative_to(tmp_path / "scratch")


def test_the_default_work_dir_sits_beside_the_model_cache() -> None:
    """The disk the model is going to live on anyway, rather than whatever TMPDIR names."""
    assert pin_model.DEFAULT_WORK_DIR.parent == Path.home() / ".cache" / "on-the-fly"


def test_the_work_dir_is_created_if_it_is_not_there(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    work_dir = tmp_path / "scratch"
    assert not work_dir.exists()

    assert run(tmp_path) == 0
    capsys.readouterr()
    assert work_dir.is_dir()


def test_the_staged_copy_does_not_outlive_the_run(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Model bytes are a DURABLE_PROJECT_ARTIFACT only in the cache, not in scratch space."""
    assert run(tmp_path) == 0
    capsys.readouterr()

    assert list((tmp_path / "scratch").iterdir()) == []


# ---------------------------------------------------------------------------------------
# What it asks the hub for.
# ---------------------------------------------------------------------------------------


def test_the_revision_is_what_is_requested(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Without it, `repo_id` means whatever the publisher last pushed."""
    assert run(tmp_path) == 0
    capsys.readouterr()

    assert hub[0]["revision"] == REVISION
    assert hub[0]["repo_id"] == "owner/some-model"


def test_only_the_files_being_pinned_are_fetched(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path) == 0
    capsys.readouterr()

    assert hub[0]["allow_patterns"] == list(FILES)


def test_the_ctranslate2_set_is_the_default(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        pin_model.main(
            ["owner/some-model", "--revision", REVISION, "--work-dir", str(tmp_path / "scratch")]
        )
        == 0
    )
    capsys.readouterr()

    assert hub[0]["allow_patterns"] == list(pin_model.DEFAULT_FILES)


# ---------------------------------------------------------------------------------------
# What it prints, which is what a reviewer pastes into the registry.
# ---------------------------------------------------------------------------------------


def printed_pin(output: str) -> dict[str, Any]:
    """Parse the emitted block back into keyword arguments, without executing it."""
    call = ast.parse(output[output.index("ModelPin(") :].strip(), mode="eval").body
    assert isinstance(call, ast.Call)
    # `arg` is None only for `**kwargs`, which this script never emits.
    return {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in call.keywords
        if keyword.arg is not None
    }


def test_what_it_prints_is_a_pin_a_reviewer_can_paste(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """It is pasted into a source file by hand, so it has to parse as one."""
    assert run(tmp_path, "--name", "some-model", "--licence", "Apache-2.0") == 0
    pin = printed_pin(capsys.readouterr().out)

    assert pin["name"] == "some-model"
    assert pin["repo_id"] == "owner/some-model"
    assert pin["revision"] == REVISION
    assert pin["licence"] == "Apache-2.0"
    assert set(pin["digests"]) == set(FILES)


def test_the_printed_digests_are_the_digests_of_what_arrived(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The one claim the pin makes. Checked against an independent hash of the same bytes."""
    import hashlib

    assert run(tmp_path) == 0
    pin = printed_pin(capsys.readouterr().out)

    for name in FILES:
        assert pin["digests"][name] == hashlib.sha256(name.encode()).hexdigest()


def test_the_name_defaults_to_the_last_path_segment(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path) == 0

    assert printed_pin(capsys.readouterr().out)["name"] == "some-model"


def test_the_pin_it_prints_is_one_ModelPin_accepts(
    tmp_path: Path, hub: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of the exercise: the output has to construct, not merely parse.

    `ModelPin` refuses a short revision and a digest that is not 64 hex characters, so this
    is the check that the block is usable rather than well-shaped.
    """
    from on_the_fly.infrastructure.model_store import ModelPin

    assert run(tmp_path) == 0
    pin = ModelPin(**printed_pin(capsys.readouterr().out))

    assert pin.is_pinned


# ---------------------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------------------


def test_a_missing_file_is_reported_rather_than_pinned_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pin naming a file that never arrived would verify nothing about it."""

    def snapshot_download(**kwargs: Any) -> str:
        Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)
        return str(kwargs["local_dir"])

    module = type(pin_model)("huggingface_hub")
    module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", module)

    assert run(tmp_path) == 1
    assert "cannot digest missing file" in capsys.readouterr().err


def test_a_hub_that_cannot_be_imported_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Since ADR 0036 the runtime requirements do contain it, so this is a broken install."""
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", None)

    assert run(tmp_path) == 1
    assert "huggingface_hub is not installed" in capsys.readouterr().err
