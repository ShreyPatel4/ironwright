"""Tenant state that survives a container restart.

Cloudflare Containers lose their disk on sleep, restart and redeploy. Everything a
tenant owns lives under tenants/<org_id>/ in the data root, so when
IRONWRIGHT_STATE_URI is set this module keeps a snapshot of that folder in an
object store (S3, R2, versitygw) or on a durable file path:

  hydrate   on the first request or job for an org on a fresh disk, pull its last
            whole generation down, verify every hash, then swap it in at once.
  persist   after every mutating request and every job, send what changed and
            commit a new generation.

Remote layout, all of it under <prefix>/tenants/<org_id>/:

  blobs/<sha256>        file bodies, immutable, named by their hash
  manifest.json         the current generation: org_id, generation, files
  manifest.prev.json    the one before it, kept whole as a fallback

A commit writes manifest.tmp.json, copies the current manifest to prev, then copies
tmp over the current one. A crash at any step leaves a manifest whose blobs exist.

Every key is built from an org id that matched ORG_ID. A manifest is untrusted on the
way in: every path part must match FILE_NAME and every blob must be a plain sha256,
or the hydrate is refused. One org's hydrate never names another org's prefix.

With no IRONWRIGHT_STATE_URI every function here returns at once and nothing is built.
The demo packs are rebuilt from code, so they are never snapshotted.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import config
from .config import FILE_NAME, ORG_ID, UnsafePath

log = logging.getLogger("ironwright.durable")

SHA = re.compile(r"[0-9a-f]{64}")
# FILE_NAME, or FILE_NAME after one dot: the engine keeps markers and lock files
# such as .source_versions.lock in a workspace. "." and ".." never match.
PART = re.compile(r"\.?" + FILE_NAME.pattern)
SIDECARS = (".wal", "-wal", "-shm", "-journal")
SQLITE_MAGIC = b"SQLite format 3\x00"
MANIFEST_VERSION = 1
MAX_DEPTH = 32
CHECKPOINT_TRIES = 20  # a reader in another process holds DuckDB's file lock briefly
CHUNK = 1 << 20


class DurableError(RuntimeError):
    """The snapshot could not be read or written whole."""


class Tampered(DurableError):
    """A manifest that names something outside its own org, refused outright."""


class Busy(DurableError):
    """Another writer holds this org."""


class Unavailable(DurableError):
    """The object store did not answer."""


# ---------------------------------------------------------------- the store

class Store:
    """Keys relative to the state prefix, over any pyarrow filesystem."""

    def __init__(self, fs: Any, base: str, local: bool) -> None:
        self.fs, self.base, self.local = fs, base.rstrip("/"), local

    def _p(self, key: str) -> str:
        return f"{self.base}/{key}"

    def _parent(self, key: str) -> None:
        if self.local:
            self.fs.create_dir(self._p(key).rsplit("/", 1)[0], recursive=True)

    def get(self, key: str) -> bytes | None:
        try:
            with self.fs.open_input_stream(self._p(key)) as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def get_to(self, key: str, dest: Path) -> str | None:
        """Stream a blob to dest, owner only. Returns its sha256, or None if missing."""
        digest = hashlib.sha256()
        try:
            src = self.fs.open_input_stream(self._p(key))
        except FileNotFoundError:
            return None
        with src, _private_file(dest) as out:
            while chunk := src.read(CHUNK):
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest()

    def put(self, key: str, data: bytes | Path) -> None:
        self._parent(key)
        with self.fs.open_output_stream(self._p(key)) as out:
            if isinstance(data, Path):
                with data.open("rb") as src:
                    while chunk := src.read(CHUNK):
                        out.write(chunk)
            else:
                out.write(data)

    def copy(self, src: str, dst: str) -> None:
        self._parent(dst)
        self.fs.copy_file(self._p(src), self._p(dst))

    def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.fs.delete_file(self._p(key))

    def exists(self, key: str) -> bool:
        from pyarrow import fs

        return self.fs.get_file_info(self._p(key)).type == fs.FileType.File

    def list(self, key: str) -> list[str]:
        from pyarrow import fs

        sel = fs.FileSelector(self._p(key), recursive=True, allow_not_found=True)
        cut = len(self.base) + 1
        return [i.path[cut:] for i in self.fs.get_file_info(sel) if i.type == fs.FileType.File]

    def delete_tree(self, key: str) -> None:
        for k in self.list(key):
            self.delete(k)
        # pyarrow leaves an empty marker object for a prefix whose last key it
        # deleted. Dropping the prefix itself takes those too.
        with contextlib.suppress(FileNotFoundError, OSError):
            self.fs.delete_dir(self._p(key))


def _open_store(uri: str) -> Store:
    from pyarrow import fs

    if uri.startswith("file://"):
        return Store(fs.LocalFileSystem(), uri.removeprefix("file://"), local=True)
    props = config.s3_properties()
    s3 = fs.S3FileSystem(access_key=props.get("s3.access-key-id"), secret_key=props.get("s3.secret-access-key"),
                         region=props["s3.region"], endpoint_override=props.get("s3.endpoint"),
                         connect_timeout=5, request_timeout=60,
                         retry_strategy=fs.AwsStandardS3RetryStrategy(max_attempts=4))
    return Store(s3, uri.removeprefix("s3://"), local=False)


_store: tuple[str, Store] | None = None
_store_lock = threading.Lock()


def enabled() -> bool:
    return config.state_uri() is not None


def store() -> Store:
    global _store
    uri = config.state_uri()
    if uri is None:
        raise DurableError("IRONWRIGHT_STATE_URI is not set")
    with _store_lock:
        if _store is None or _store[0] != uri:
            _store = (uri, _open_store(uri))
        return _store[1]


def reset_store() -> None:
    """Forget the built store, so the next call reads the environment again."""
    global _store
    with _store_lock:
        _store = None


# ---------------------------------------------------------------- local bookkeeping

def _org(org_id: str) -> str:
    if not isinstance(org_id, str) or not ORG_ID.fullmatch(org_id):
        raise UnsafePath(f"unsafe org id: {org_id!r}")
    config.tenant_dir(org_id)  # the same rules as every other road to the folder
    return org_id


def _state_dir() -> Path:
    """Outside tenants/, and dot named, so no tenant route can name it."""
    config.secure_data_root()
    return config.private_dir(config.data_root() / ".durable")


def _marker(org_id: str) -> Path:
    return _state_dir() / f"{org_id}.json"


def _read_marker(org_id: str) -> dict[str, Any] | None:
    path = config.data_root() / ".durable" / f"{org_id}.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _generation_file(org_id: str) -> Path:
    return config.data_root() / ".durable" / f"{org_id}.gen"


def _local_generation(org_id: str) -> int | None:
    """Every request asks this, so it reads a few bytes, never the manifest."""
    try:
        return int(_generation_file(org_id).read_text())
    except FileNotFoundError:
        return None


def _write_marker(org_id: str, generation: int, manifest: dict[str, Any], index: dict[str, list]) -> None:
    """The full state first, the generation last: a disk with a .gen has a whole marker."""
    for path, body in ((_marker(org_id), json.dumps({"generation": generation, "manifest": manifest, "index": index})),
                       (_state_dir() / f"{org_id}.gen", str(generation))):
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
        config.write_private(tmp, body)
        os.replace(tmp, path)


@contextlib.contextmanager
def _private_file(path: Path) -> Iterator[Any]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        yield fh


# ---------------------------------------------------------------- the org lock

_held = threading.local()


@contextlib.contextmanager
def org_lock(org_id: str, timeout: float | None = None) -> Iterator[None]:
    """One writer per org across every process on this disk. Reentrant in a thread,
    so a job that holds it can persist. The kernel drops it if the holder dies."""
    if not enabled():
        yield
        return
    _org(org_id)
    counts: dict[str, int] = getattr(_held, "counts", None) or {}
    _held.counts = counts
    if counts.get(org_id):
        counts[org_id] += 1
        try:
            yield
        finally:
            counts[org_id] -= 1
        return
    locks = config.private_dir(_state_dir() / "locks")
    fd = os.open(locks / f"{org_id}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise Busy(f"org {org_id} is held by another writer") from None
                time.sleep(0.05)
        counts[org_id] = 1
        try:
            yield
        finally:
            counts[org_id] = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- manifests

def _key(org_id: str, name: str) -> str:
    return f"tenants/{org_id}/{name}"


def _check_rel(rel: Any) -> list[str]:
    if not isinstance(rel, str):
        raise Tampered("a manifest path is not a string")
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH or not all(PART.fullmatch(p) for p in parts):
        raise Tampered(f"manifest path refused: {rel!r}")
    return parts


def _check_manifest(org_id: str, raw: bytes) -> dict[str, Any]:
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise Tampered("manifest is not json") from e
    if not isinstance(m, dict) or m.get("version") != MANIFEST_VERSION:
        raise Tampered("manifest has an unknown shape")
    if m.get("org_id") != org_id:
        raise Tampered("manifest names another org")
    gen, files = m.get("generation"), m.get("files")
    if not isinstance(gen, int) or gen < 1 or not isinstance(files, dict):
        raise Tampered("manifest has an unknown shape")
    for rel, entry in files.items():
        _check_rel(rel)
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) \
                or not SHA.fullmatch(entry["sha256"]):
            raise Tampered(f"manifest blob refused for {rel!r}")
        if not isinstance(entry.get("mode"), int) or not isinstance(entry.get("size"), int):
            raise Tampered(f"manifest entry refused for {rel!r}")
    return m


def _target(root: Path, parts: list[str]) -> Path:
    path = root
    for part in parts[:-1]:
        path = config.private_dir(config.safe_child(path, part, PART))
    return config.safe_child(path, parts[-1], PART)


# ---------------------------------------------------------------- hydrate

def ensure_hydrated(org_id: str) -> int | None:
    """The org's generation on this disk, pulling it down first if this disk has
    never seen it. None when durable state is off."""
    if not enabled():
        return None
    _org(org_id)
    found = _local_generation(org_id)
    if found is not None:
        return found
    with org_lock(org_id):
        found = _local_generation(org_id)
        if found is not None:
            return found
        return _hydrate(org_id)


def _hydrate(org_id: str) -> int:
    s = store()
    raws: list[bytes] = []
    try:
        for name in ("manifest.json", "manifest.prev.json"):
            raw = s.get(_key(org_id, name))
            if raw is not None:
                raws.append(raw)
    except OSError as e:
        raise Unavailable(f"object store did not answer: {type(e).__name__}") from e
    if not raws:
        _write_marker(org_id, 0, {}, {})
        return 0
    manifests = [_check_manifest(org_id, raw) for raw in raws]  # any tamper refuses the org
    problems = []
    for manifest in manifests:
        try:
            index = _pull(org_id, manifest)
        except (DurableError, OSError) as e:
            problems.append(f"generation {manifest['generation']}: {e}")
            continue
        _write_marker(org_id, manifest["generation"], manifest, index)
        log.info("hydrated org %s at generation %s", org_id, manifest["generation"])
        return manifest["generation"]
    raise DurableError("no whole generation to hydrate: " + "; ".join(problems))


def _pull(org_id: str, manifest: dict[str, Any]) -> dict[str, list]:
    """Download one generation beside the tenant folder, then swap it in."""
    s = store()
    staging_root = config.private_dir(_state_dir() / "staging")
    staging = config.private_dir(staging_root / f"{org_id}-{uuid.uuid4().hex}")
    try:
        index: dict[str, list] = {}
        modes: list[tuple[Path, int]] = []
        for rel, entry in manifest["files"].items():
            dest = _target(staging, _check_rel(rel))
            got = s.get_to(_key(org_id, f"blobs/{entry['sha256']}"), dest)
            if got is None:
                raise DurableError(f"blob missing for {rel}")
            if got != entry["sha256"]:
                raise DurableError(f"blob does not match its hash for {rel}")
            modes.append((dest, entry["mode"] & 0o700))
            index[rel] = [entry["sha256"]]
        for dest, mode in modes:
            dest.chmod(mode)
        target = config.tenant_dir(org_id)
        config.private_dir(config.tenants_root())
        old = staging_root / f"{org_id}-old-{uuid.uuid4().hex}"
        if target.exists():
            # This disk never saw the org, so anything already here was written
            # since the restart and is newer than the snapshot. It wins.
            for here in _local_files(target):
                rel = here.relative_to(target).as_posix()
                dest = _target(staging, _check_rel(rel))
                os.replace(here, dest)
                index.pop(rel, None)
            os.rename(target, old)
        os.rename(staging, target)
        shutil.rmtree(old, ignore_errors=True)
        for rel in list(index):
            st = (target / rel).stat()
            sig = _signature(target / rel, st)
            if sig is None:
                index.pop(rel)
            else:
                index[rel] = [*sig, index[rel][0]]
        return index
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------- persist

def persist(org_id: str) -> int | None:
    """Send what changed under tenants/<org_id>/ and commit a generation. Returns the
    generation now committed. None when durable state is off."""
    if not enabled():
        return None
    _org(org_id)
    with org_lock(org_id):
        ensure_hydrated(org_id)
        state = _read_marker(org_id) or {"generation": 0, "manifest": {}, "index": {}}
        gen = int(state["generation"])
        prev = state["manifest"].get("files", {}) if state["manifest"] else {}
        root = config.tenant_dir(org_id)
        if not root.exists():
            if prev:
                raise DurableError(f"tenant folder for {org_id} is missing; forget() is the only way to drop an org")
            return gen
        files, index = _scan(org_id, root, state["index"], prev)
        if files == prev:
            _write_marker(org_id, gen, state["manifest"], index)
            return gen
        manifest = {"version": MANIFEST_VERSION, "org_id": org_id, "generation": gen + 1,
                    "at": round(time.time(), 3), "files": files}
        _commit(org_id, manifest, prev)
        _write_marker(org_id, gen + 1, manifest, index)
        return gen + 1


def _commit(org_id: str, manifest: dict[str, Any], prev: dict[str, Any]) -> None:
    s = store()
    body = json.dumps(manifest, sort_keys=True).encode()
    s.put(_key(org_id, "manifest.tmp.json"), body)
    if s.exists(_key(org_id, "manifest.json")):
        s.copy(_key(org_id, "manifest.json"), _key(org_id, "manifest.prev.json"))
    s.copy(_key(org_id, "manifest.tmp.json"), _key(org_id, "manifest.json"))
    s.delete(_key(org_id, "manifest.tmp.json"))
    keep = {e["sha256"] for e in manifest["files"].values()} | {e["sha256"] for e in prev.values()}
    for key in s.list(_key(org_id, "blobs")):
        if key.rsplit("/", 1)[-1] not in keep:
            s.delete(key)


def _scan(org_id: str, root: Path, index: dict[str, list],
          prev: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list]]:
    s = store()
    known = {e["sha256"] for e in prev.values()}
    files: dict[str, Any] = {}
    new_index: dict[str, list] = {}
    tmp_dir = config.private_dir(_state_dir() / "staging")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            if (here / name).is_symlink():
                raise DurableError(f"cannot carry a symlink: {(here / name).relative_to(root)}")
        for name in sorted(filenames):
            path = here / name
            rel = path.relative_to(root).as_posix()
            if name.endswith(SIDECARS):
                continue
            try:
                _check_rel(rel)
            except Tampered:
                raise DurableError(f"the manifest cannot carry this file name: {rel!r}") from None
            st = path.stat()
            mode = st.st_mode & 0o777
            sig = _signature(path, st)
            cached = index.get(rel)
            if sig is not None and cached and cached[:-1] == sig:
                files[rel] = {"sha256": cached[-1], "size": st.st_size, "mode": mode}
                new_index[rel] = cached
                continue
            tmp = tmp_dir / f"snap-{uuid.uuid4().hex}"
            try:
                sha = _snapshot(path, tmp)
                if sha is None:  # a writer is mid-flight; keep what we had
                    if rel in prev:
                        files[rel] = prev[rel]
                    log.warning("durable: %s is busy, kept the previous copy", rel)
                    continue
                if sha not in known:
                    s.put(_key(org_id, f"blobs/{sha}"), tmp)
                    known.add(sha)
                files[rel] = {"sha256": sha, "size": tmp.stat().st_size, "mode": mode}
                if sig is not None:
                    new_index[rel] = [*sig, sha]
            finally:
                tmp.unlink(missing_ok=True)
    return files, new_index


def _signature(path: Path, st: os.stat_result) -> list[int] | None:
    """What says a file is unchanged since its hash was taken. A database counts its WAL
    too, since a commit can move only the WAL. A DuckDB file with a pending WAL is never
    taken as unchanged: it has to be checkpointed first."""
    sig = [st.st_size, st.st_mtime_ns]
    if not _is_live_db(path):
        return sig
    if path.suffix == ".duckdb":
        return None if _wal_pending(path) else sig
    try:
        wal = path.with_name(f"{path.name}-wal").stat()
        return [*sig, wal.st_size, wal.st_mtime_ns]
    except FileNotFoundError:
        return [*sig, 0, 0]


def _local_files(root: Path) -> list[Path]:
    return [Path(d) / f for d, _, names in os.walk(root) for f in names
            if not (Path(d) / f).is_symlink() and not f.endswith(SIDECARS)]


def _is_live_db(path: Path) -> bool:
    if path.suffix == ".duckdb":
        return True
    try:
        with path.open("rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _snapshot(path: Path, dest: Path) -> str | None:
    """A consistent copy of path at dest and its sha256, or None if it is mid-write."""
    if path.suffix == ".duckdb":
        if not _checkpoint_duckdb(path):
            return None
    elif _is_live_db(path):
        _backup_sqlite(path, dest)
        return _hash_file(dest)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as src, _private_file(dest) as out:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            out.write(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    if path.suffix == ".duckdb" and _wal_pending(path):
        return None
    return digest.hexdigest()


def _wal_pending(path: Path) -> bool:
    wal = path.with_name(f"{path.name}.wal")
    try:
        return wal.stat().st_size > 0
    except FileNotFoundError:
        return False


def _checkpoint_duckdb(path: Path) -> bool:
    """Fold the WAL into the file. No WAL means the last writer closed cleanly."""
    if not _wal_pending(path):
        return True
    import duckdb

    for _ in range(CHECKPOINT_TRIES):
        try:
            con = duckdb.connect(str(path))
            try:
                con.execute("CHECKPOINT")
            finally:
                con.close()
            return not _wal_pending(path)
        except duckdb.Error:
            time.sleep(0.1)
    return False


def _backup_sqlite(path: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        with _private_file(dest):
            pass
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- forget

def forget(org_id: str) -> None:
    """Drop an org's snapshot and this disk's marker. Only the org deletion job calls this."""
    if not enabled():
        return
    _org(org_id)
    with org_lock(org_id):
        store().delete_tree(f"tenants/{org_id}")
        _generation_file(org_id).unlink(missing_ok=True)
        _marker(org_id).unlink(missing_ok=True)


# ---------------------------------------------------------------- hooks

PERSIST_WAIT_SECONDS = 2.0  # past this a job holds the org; its own persist will carry the change


def _report(event: str, org_id: str, e: Exception) -> None:
    """The type only: an S3 error message carries the endpoint and the bucket."""
    from .obs.logging import emit

    emit(event, level=logging.ERROR, org_id=org_id, error_type=type(e).__name__)


def persist_quietly(org_id: str, wait: float | None = PERSIST_WAIT_SECONDS) -> None:
    """Persist without failing the caller. If a job holds the org, finish in the background."""
    try:
        with org_lock(org_id, timeout=wait):
            persist(org_id)
    except Busy:
        threading.Thread(target=persist_quietly, args=(org_id, None), daemon=True,
                         name=f"persist-{org_id[:8]}").start()
    except Exception as e:  # noqa: BLE001  the request already happened; say so and retry next write
        _report("durable.persist_failed", org_id, e)


def hydrate_for(request: Any, org_id: str) -> None:
    """ctx()'s hook: bring this org's folder back before any route reads it, and note
    the org so the write middleware knows what to persist."""
    if not enabled():
        return
    request.state.durable_org = org_id
    try:
        ensure_hydrated(org_id)
    except DurableError as e:
        from fastapi import HTTPException

        _report("durable.hydrate_failed", org_id, e)
        raise HTTPException(503, "workspace storage is not ready; try again shortly") from None


class PersistAfterWrite:
    """After a write request succeeds, persist its org before the answer goes out, so
    a 2xx means the change is in the object store. Reads pass straight through."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS") or not enabled():
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})

        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                org_id = state.get("durable_org")
                if org_id:
                    from starlette.concurrency import run_in_threadpool

                    await run_in_threadpool(persist_quietly, org_id)
            await send(message)

        await self.app(scope, receive, wrapped)


@contextlib.contextmanager
def job_scope(org_id: str) -> Iterator[None]:
    """A job owns its org from claim to finish: nothing else writes or persists it meanwhile."""
    with org_lock(org_id):
        ensure_hydrated(org_id)
        yield


def after_job(org_id: str, kind: str, state: str) -> None:
    """Inside job_scope, before the job is marked finished."""
    if not enabled():
        return
    if kind == "org_delete" and state == "succeeded":
        try:
            forget(org_id)
        except Exception as e:  # noqa: BLE001
            _report("durable.forget_failed", org_id, e)
        return
    persist_quietly(org_id, wait=None)
