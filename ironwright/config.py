"""Paths and settings. Everything lives under one data directory so a run is easy to wipe or ship."""
from __future__ import annotations

import os
import re
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PACKS_DIR = PACKAGE_DIR / "packs"

# One path component, never a dot first, never a slash, backslash or percent
# sign. A percent sign is refused so nothing downstream can decode a name into
# a traversal. FILE_NAME is the wider spelling real files on disk arrive in.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
FILE_NAME = re.compile(r"\w[\w. ()+,=@-]{0,254}")


class UnsafePath(ValueError):
    """A name that would not stay a plain child of its root."""


def data_root() -> Path:
    return Path(os.environ.get("IRONWRIGHT_DATA", Path.cwd() / "data")).resolve()


def safe_child(root: Path, name: str, pattern: re.Pattern[str] = SAFE_NAME) -> Path:
    """The one road from a request value to a path: root/name or UnsafePath.

    The resolved child must equal root/name, not merely sit under root. A
    symlink to a sibling workspace is inside the root and still an escape.
    """
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise UnsafePath(f"unsafe name: {name!r}")
    child = Path(root).resolve() / name
    try:
        if child.resolve() != child:
            raise UnsafePath(f"{name!r} is a symlink, refused")
    except (OSError, RuntimeError) as e:  # a symlink loop
        raise UnsafePath(f"{name!r} does not resolve: {e}") from e
    return child


def private_dir(path: Path) -> Path:
    """Create path owner-only. mkdir(mode=) is masked by umask, skips parents
    and ignores existing folders, so this chmods explicitly. Parents that
    already exist are not ours to change."""
    missing = [path]
    while not missing[-1].parent.exists():
        missing.append(missing[-1].parent)
    for d in reversed(missing):
        d.mkdir(exist_ok=True)
        d.chmod(0o700)
    return path


def write_private(path: Path, data: str | bytes) -> None:
    """Write an owner-only file: 0600 from the first byte, never through a symlink."""
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)  # the file may have existed, wider
        fh.write(raw)


def _tighten(path: Path, mode: int) -> None:
    if path.exists() and not path.is_symlink():
        path.chmod(mode)


def secure_data_root() -> Path:
    """Owner-only data root and credentials. A root the user pointed at a
    shared place (/tmp, their home folder) keeps its mode: locking everyone
    out of /tmp is not a security feature. Its children still go private."""
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    if not root.stat().st_mode & stat.S_ISVTX and root != Path.home().resolve():
        try:
            root.chmod(0o700)
        except PermissionError:
            warnings.warn(f"data root {root} is not ours to chmod; left as is", stacklevel=2)
    _tighten(root / "auth", 0o700)
    _tighten(root / "auth" / "credentials.json", 0o600)
    return root


@dataclass(frozen=True)
class PackPaths:
    """Where each layer of one pack's run is stored. Mirrors the four-layer stack."""

    pack: str

    @property
    def root(self) -> Path:
        return safe_child(data_root(), self.pack)

    @property
    def legacy(self) -> Path:          # RAW SOURCES: what the old systems emit
        return self.root / "legacy"

    @property
    def lake(self) -> Path:            # LOGIC layer storage: Iceberg medallion
        return self.root / "lake"

    @property
    def ontology_db(self) -> Path:     # ONTOLOGY layer: objects, links, actions
        return self.root / "ontology.duckdb"

    @property
    def checkpoints(self) -> Path:     # APPLICATION layer: agent thread state
        return self.root / "checkpoints.sqlite"

    @property
    def traces(self) -> Path:
        return self.root / "traces"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def ground_truth(self) -> Path:    # what the generator planted, used only by evals
        return self.legacy / "_ground_truth.json"

    def report(self, name: str) -> Path:
        return safe_child(self.reports, f"{name}.json")

    def ensure(self) -> PackPaths:
        secure_data_root()
        for p in (self.root, self.legacy, self.lake, self.traces, self.reports):
            private_dir(p)
        # The audit log lives in the ontology store. The engine creates these
        # files itself, after the root above is already closed to other users.
        for f in (self.ontology_db, self.checkpoints):
            for sidecar in (f, f.with_name(f"{f.name}.wal"), f.with_name(f"{f.name}-wal"),
                            f.with_name(f"{f.name}-shm"), f.with_name(f"{f.name}-journal")):
                _tighten(sidecar, 0o600)
        return self


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"IRONWRIGHT_{name}", default)
