"""Pinned, integrity-verified model files.

A speech model is executable trust in the same way a package is (Article 12): it is
fetched over a network, it is large, it is opaque, and it decides what the application
says. Unlike a package it is fetched at *runtime*, on the user's machine, long after any
review happened — so the verification has to live here.

Three rules, all fail-closed:

**An unpinned model is refused.** A `ModelPin` carries the expected SHA-256 of every file
it needs. A model with no recorded digests cannot be loaded, no matter how convenient that
would be. `compute_digests()` exists so a maintainer can produce the pin deliberately, in
a reviewed commit — not so the loader can invent one at runtime.

**A mismatch is refused and left alone.** A file whose digest does not match is never used
and never silently re-downloaded. It is also not deleted: quietly destroying the evidence
of a possible supply-chain event is the wrong reflex (Article 10). The path is reported so
a human can look at it.

**Downloading is opt-in.** `allow_download` defaults to false. Reaching the network is a
distinct capability from reading a model that is already present, and the default is the
one that does not.

Model files are `DURABLE_PROJECT_ARTIFACT`, not project content. They are intentionally
persistent and carry nothing anyone said, so the ten-second rule does not apply to them —
but nothing derived from user audio may ever be written into this cache.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Read in chunks: a model file is hundreds of megabytes and hashing it must not become a
# memory decision (handbook 64I).
_HASH_CHUNK_BYTES = 1024 * 1024


class ModelStoreError(Exception):
    """The model could not be located, fetched, or trusted."""


class ModelIntegrityError(ModelStoreError):
    """A model file did not match its pinned digest.

    Treated as a security event, not a cache miss. The file is left in place for
    inspection and is never used.
    """


class ModelNotPresentError(ModelStoreError):
    """The model is not in the cache and downloading was not permitted."""


@dataclass(frozen=True)
class ModelPin:
    """A specific model, pinned to a revision and to file digests.

    `revision` is a git commit on the model repository. It is what makes `repo_id` refer to
    one immutable tree rather than to whatever the publisher last pushed — pinning the name
    alone would leave the content free to change under us.

    `digests` is the independent check. The revision says which tree was asked for; the
    digests confirm which bytes arrived.
    """

    name: str
    repo_id: str
    revision: str
    licence: str
    digests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for required in ("name", "repo_id", "revision", "licence"):
            if not str(getattr(self, required)).strip():
                raise ValueError(f"model pin is missing {required!r}")
        if len(self.revision) < 7:
            raise ValueError(
                f"model pin revision {self.revision!r} is too short to identify a commit"
            )
        for filename, digest in self.digests.items():
            if len(digest) != 64:
                raise ValueError(f"digest for {filename!r} is not a SHA-256 hex string")

    @property
    def is_pinned(self) -> bool:
        return bool(self.digests)

    def __str__(self) -> str:
        return f"{self.name} ({self.repo_id}@{self.revision[:12]})"


def file_digest(path: Path) -> str:
    """SHA-256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def compute_digests(directory: Path, filenames: list[str]) -> dict[str, str]:
    """Digest the named files, for a maintainer producing a pin.

    Deliberately a separate function that a human calls, not something the loader can reach.
    A loader that computed its own expected values would verify nothing at all.
    """
    resolved = directory.resolve()
    computed: dict[str, str] = {}
    for filename in filenames:
        path = (resolved / filename).resolve()
        if not path.is_relative_to(resolved):
            raise ModelStoreError(f"refusing to digest outside the model directory: {filename}")
        if not path.is_file():
            raise ModelStoreError(f"cannot digest missing file: {path}")
        computed[filename] = file_digest(path)
    return computed


class ModelStore:
    """Resolves a `ModelPin` to a verified directory on disk."""

    def __init__(self, cache_dir: Path | str, *, allow_download: bool = False) -> None:
        self._cache_dir = Path(cache_dir).expanduser().resolve()
        self._allow_download = allow_download

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def allow_download(self) -> bool:
        return self._allow_download

    def __repr__(self) -> str:
        return f"ModelStore(cache_dir={self._cache_dir!s}, allow_download={self._allow_download})"

    def local_path(self, pin: ModelPin) -> Path:
        """Where this pin lives. Keyed by revision, so two revisions never share a directory."""
        return self._cache_dir / pin.name / pin.revision

    def ensure(self, pin: ModelPin) -> Path:
        """Return a verified local directory for `pin`, downloading only if permitted.

        Raises rather than returning an unverified path. Every caller of this gets either a
        directory whose contents matched the pin, or an exception.
        """
        if not pin.is_pinned:
            raise ModelIntegrityError(
                f"model {pin} declares no file digests and cannot be verified. "
                "Produce a pin with compute_digests() and commit it; an unpinned model is "
                "refused rather than trusted."
            )

        target = self.local_path(pin)
        if not self._is_complete(pin, target):
            if not self._allow_download:
                raise ModelNotPresentError(
                    f"model {pin} is not in {target} and downloading is not enabled. "
                    "Fetch it explicitly, or point the store at a directory that has it."
                )
            self._download(pin, target)

        self.verify(pin, target)
        return target

    def _is_complete(self, pin: ModelPin, target: Path) -> bool:
        return target.is_dir() and all((target / name).is_file() for name in pin.digests)

    def verify(self, pin: ModelPin, directory: Path) -> None:
        """Check every pinned file. Raises `ModelIntegrityError` on the first mismatch."""
        resolved = directory.resolve()
        for filename, expected in pin.digests.items():
            path = (resolved / filename).resolve()
            if not path.is_relative_to(resolved):
                raise ModelIntegrityError(
                    f"model pin names a path outside its own directory: {filename!r}"
                )
            if not path.is_file():
                raise ModelIntegrityError(f"model {pin} is missing {filename!r} at {path}")

            actual = file_digest(path)
            if actual != expected:
                # Left in place on purpose. Deleting it would destroy the evidence of what
                # may be a supply-chain event, and re-downloading over it would hide that
                # anything happened at all.
                raise ModelIntegrityError(
                    f"model {pin} failed verification for {filename!r}: expected "
                    f"{expected[:16]}..., got {actual[:16]}.... The file has been left at "
                    f"{path} for inspection and was not used."
                )

    def _download(self, pin: ModelPin, target: Path) -> None:
        """Fetch the pinned revision from the model hub.

        Imported lazily so that a machine with a populated cache never needs the hub client,
        and so the domain and its tests run without it.
        """
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelStoreError(
                "huggingface_hub is required to download a model; install the runtime "
                f"requirements. Underlying error: {exc}"
            ) from exc

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_download(
                repo_id=pin.repo_id,
                # The revision is the whole point: without it the repository name refers to
                # whatever was pushed most recently.
                revision=pin.revision,
                local_dir=str(target),
                allow_patterns=list(pin.digests),
            )
        except Exception as exc:
            raise ModelStoreError(f"could not download model {pin}: {exc}") from exc
