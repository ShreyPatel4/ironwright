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
# Tenancy contract: org ids are uuid4().hex, workspace slugs read like org slugs.
ORG_ID = re.compile(r"[0-9a-f]{32}")
WS_SLUG = re.compile(r"[a-z][a-z0-9-]{2,39}")

# workspaces.py writes this into a scenario organization it may later rmtree.
# A platform folder holding one belongs to that organization, so nothing of
# ours may land in it.
_SCENARIO_MARKER = ".ironwright-workspace"


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


def _platform_dir(name: str) -> Path:
    d = safe_child(data_root(), name)
    marker = d / _SCENARIO_MARKER
    if marker.exists() or marker.is_symlink():
        raise UnsafePath(f"{name} is held by a scenario organization")
    return d


def demo_root() -> Path:
    return _platform_dir("demo")


def tenants_root() -> Path:
    return _platform_dir("tenants")


def tenant_dir(org_id: str) -> Path:
    return safe_child(tenants_root(), org_id, ORG_ID)


def uploads_root(org_id: str) -> Path:
    return safe_child(tenant_dir(org_id), "uploads")


def workspaces_root(org_id: str) -> Path:
    return safe_child(tenant_dir(org_id), "workspaces")


def workspace_dir(org_id: str, slug: str) -> Path:
    return safe_child(workspaces_root(org_id), slug, WS_SLUG)


def ensure_tenant(org_id: str) -> Path:
    """Close one org's tree to other users, even where a folder already exists
    with wider modes. Run this before writing anything under a tenant."""
    secure_data_root()
    root = private_dir(tenants_root())
    root.chmod(0o700)
    org = private_dir(tenant_dir(org_id))
    org.chmod(0o700)
    for d in (uploads_root(org_id), workspaces_root(org_id)):
        private_dir(d).chmod(0o700)
    return org


def upload_file(org_id: str, ref: str) -> Path:
    """<upload_id>/<file> under this org's uploads, or UnsafePath. Exactly two
    parts, each a plain child, so a ref cannot climb or name another org."""
    parts = ref.split("/") if isinstance(ref, str) else []
    if len(parts) != 2:
        raise UnsafePath(f"unsafe upload ref: {ref!r}")
    return safe_child(safe_child(uploads_root(org_id), parts[0]), parts[1], FILE_NAME)


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
    run_root: Path | None = None   # a tenant workspace's run/; demo packs live under demo/

    @property
    def root(self) -> Path:
        if self.run_root is None:
            return safe_child(demo_root(), self.pack)
        if self.run_root.resolve() != self.run_root:
            raise UnsafePath(f"{self.pack} run folder is a symlink, refused")
        return self.run_root

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


# Every hosted setting is an environment variable, documented in docs/deploy-hosted.md.
def db_url() -> str:
    """The metadata store. Bare postgres URLs name the driver we ship: left alone,
    SQLAlchemy would reach for psycopg2, which the image does not have."""
    url = setting("DB_URL") or f"sqlite:///{data_root() / 'meta.sqlite'}"
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)


def lake_uri() -> str | None:
    """s3://bucket[/prefix] with no trailing slash, or None for a lake on local disk."""
    uri = setting("LAKE_URI")
    if not uri:
        return None
    if not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]+(/[\w./-]*)?", uri):
        raise ValueError("IRONWRIGHT_LAKE_URI must look like s3://bucket/prefix")
    return uri.rstrip("/")


def s3_properties() -> dict[str, str]:
    """PyIceberg file IO settings. Keys come from the standard AWS variables, and
    what is unset is left out so the driver's own credential chain still applies."""
    props = {"s3.region": os.environ.get("AWS_REGION", "us-east-1")}
    for key, value in (("s3.endpoint", setting("S3_ENDPOINT")),
                       ("s3.access-key-id", os.environ.get("AWS_ACCESS_KEY_ID")),
                       ("s3.secret-access-key", os.environ.get("AWS_SECRET_ACCESS_KEY"))):
        if value:
            props[key] = value
    return props


def bind_host() -> str:
    """Loopback unless the environment says otherwise. A container states 0.0.0.0."""
    return setting("HOST", "127.0.0.1") or "127.0.0.1"


def bind_port() -> int:
    return int(setting("PORT") or os.environ.get("PORT") or 8000)
