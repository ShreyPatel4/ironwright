"""Storing an upload: stream to disk, caps while streaming, write once.

Files land in `uploads/<id>.partial/` and the folder is renamed to `uploads/<id>/`
only when the whole request was good. A refused or cut off request leaves nothing,
and a partial folder can never be named by a request, because an upload id is 32
hex characters and nothing else.

I parse the multipart body here rather than take FastAPI's UploadFile. UploadFile
spools the whole body to temp storage before a handler runs, so a cap checked there
is checked too late.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from ..config import (
    FILE_NAME,
    UnsafePath,
    data_root,
    private_dir,
    safe_child,
    secure_data_root,
    setting,
    write_private,
)
from ..connectors import _MAX_FILE_BYTES, _MAX_FILES

# Until T1 lands, every session maps to this one org. See routes.current_org.
DEV_ORG = "dev-org"

ALLOWED_SUFFIXES = {".csv", ".txt", ".dat", ".tsv"}
UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
MANIFEST = "manifest.json"

_CHUNK = 64 * 1024
_MAX_PART_HEADER_BYTES = 16 * 1024
_MAX_CONTROL_SHARE = 0.01
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
         (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))
_BINARY_MAGIC = (b"%PDF-", b"PK\x03\x04", b"\x1f\x8b", b"\x89PNG", b"\x7fELF", b"\xd0\xcf\x11\xe0")
_TEXT_CONTROLS = set("\t\n\r\f\x1a")
_CONTROL_BYTES = bytes(b for b in [*range(1, 32), 127] if chr(b) not in _TEXT_CONTROLS)


class UploadRefused(ValueError):
    """A request or a file the upload route turns away. Status is the HTTP answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def max_file_bytes() -> int:
    return int(setting("L1_MAX_FILE_BYTES", _MAX_FILE_BYTES))


def max_files() -> int:
    return int(setting("L1_MAX_FILES", _MAX_FILES))


def _uploads_root(org_id: str) -> Path:
    return safe_child(safe_child(safe_child(data_root(), "tenants"), org_id), "uploads")


def upload_dir(org_id: str, upload_id: str) -> Path:
    """The folder of one committed upload. Refuses anything but 32 hex characters."""
    return safe_child(_uploads_root(org_id), upload_id, pattern=UPLOAD_ID)


def manifest(org_id: str, upload_id: str) -> dict[str, Any]:
    """{id, files} of a committed upload, or FileNotFoundError."""
    return json.loads((upload_dir(org_id, upload_id) / MANIFEST).read_text())


def sniff_text(path: Path) -> None:
    """Refuse bytes that are not text. Reads the whole file: a text head over a
    binary tail is still binary. A latin-1 decode proves nothing, it never fails."""
    with open(path, "rb") as fh:
        head = fh.read(_CHUNK)
        if head.startswith(_BINARY_MAGIC):
            raise UploadRefused(415, f"{path.name} is a binary format, not text")
        wide = next((enc for bom, enc in _BOMS if head.startswith(bom)), None)
        if wide is None and _looks_like_ebcdic(head):
            raise UploadRefused(415, f"{path.name} looks like EBCDIC. Convert it to ASCII or UTF-8 first")
        decoder = codecs.getincrementaldecoder(wide)() if wide else None
        total = controls = 0
        chunk = head
        while chunk:
            if decoder:
                try:
                    text = decoder.decode(chunk)
                except UnicodeDecodeError as e:
                    raise UploadRefused(415, f"{path.name} is not valid {wide}") from e
                nul = "\x00" in text
                total += len(text)
                controls += sum(1 for c in text if (ord(c) < 32 or ord(c) == 127) and c not in _TEXT_CONTROLS)
            else:
                nul = b"\x00" in chunk
                total += len(chunk)
                controls += len(chunk) - len(chunk.translate(None, _CONTROL_BYTES))
            if nul:
                raise UploadRefused(415, f"{path.name} holds NUL bytes, so it is not text")
            chunk = fh.read(_CHUNK)
    if total and controls / total > _MAX_CONTROL_SHARE:
        raise UploadRefused(415, f"{path.name} is full of control characters, so it is not text")


def _looks_like_ebcdic(head: bytes) -> bool:
    """Mostly high bytes that fail as UTF-8 but read as plain ASCII under cp037.
    Letting it through would profile as confident garbage."""
    if not head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
        return False
    except UnicodeDecodeError:
        pass
    if sum(b >= 0x80 for b in head) / len(head) < 0.3:
        return False
    text = head.decode("cp037")
    return sum(c.isascii() and (c.isprintable() or c in "\r\n\t") for c in text) / len(text) >= 0.95


class UploadSession:
    """One request's files, staged until commit. Each file is created exclusive,
    hashed as it streams, then made read only."""

    def __init__(self, org_id: str) -> None:
        secure_data_root()
        root = _uploads_root(org_id)
        self.upload_id = uuid.uuid4().hex
        self._final = safe_child(root, self.upload_id, pattern=UPLOAD_ID)
        self._staging = safe_child(root, f"{self.upload_id}.partial")
        private_dir(self._staging)
        self._files: list[dict[str, Any]] = []
        self._names: set[str] = set()
        self._open: Any = None
        self._path: Path | None = None
        self._hash: Any = None
        self._bytes = 0
        self._cap = max_file_bytes()

    def begin_file(self, name: str) -> None:
        try:
            path = safe_child(self._staging, name, pattern=FILE_NAME)
        except UnsafePath as e:
            raise UploadRefused(400, "a file name must be one plain name") from e
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise UploadRefused(415, f"only {', '.join(sorted(ALLOWED_SUFFIXES))} files are allowed")
        if name in self._names:
            raise UploadRefused(400, f"{name} was sent more than once")
        if len(self._names) >= max_files():
            raise UploadRefused(413, f"an upload holds at most {max_files()} files")
        self._names.add(name)
        self._path = path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._open = os.fdopen(fd, "wb")
        self._hash = hashlib.sha256()
        self._bytes = 0

    def write(self, data: bytes) -> None:
        self._bytes += len(data)
        if self._bytes > self._cap:
            raise UploadRefused(413, f"a file may be at most {self._cap} bytes")
        self._hash.update(data)
        self._open.write(data)

    def end_file(self) -> None:
        self._open.close()
        self._open = None
        path = self._path
        assert path is not None
        if self._bytes == 0:
            raise UploadRefused(400, f"{path.name} is empty")
        sniff_text(path)
        path.chmod(0o400)
        self._files.append({"name": path.name, "bytes": self._bytes, "sha256": self._hash.hexdigest()})

    def commit(self) -> dict[str, Any]:
        if not self._files:
            raise UploadRefused(400, "the request held no files")
        out = {"id": self.upload_id, "files": self._files}
        write_private(self._staging / MANIFEST, json.dumps(out, indent=1))
        os.rename(self._staging, self._final)
        return out

    def abort(self) -> None:
        if self._open:
            self._open.close()
            self._open = None
        shutil.rmtree(self._staging, ignore_errors=True)


class MultipartSink:
    """Feeds a multipart/form-data stream into an UploadSession, one chunk at a
    time, so a cap trips as soon as the bytes that break it arrive."""

    def __init__(self, session: UploadSession, content_type: str) -> None:
        kind, options = parse_options_header(content_type)
        boundary = options.get(b"boundary")
        if kind != b"multipart/form-data" or not boundary:
            raise UploadRefused(415, "send the files as multipart/form-data")
        self._session = session
        self._field = self._value = b""
        self._headers: dict[bytes, bytes] = {}
        self._header_bytes = 0
        self._ended = False
        self._parser = MultipartParser(boundary, callbacks={
            "on_part_begin": self._part_begin,
            "on_part_data": self._part_data,
            "on_part_end": session.end_file,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_end": self._end,
        })

    def write(self, chunk: bytes) -> None:
        try:
            self._parser.write(chunk)
        except MultipartParseError as e:
            raise UploadRefused(400, "the multipart body is malformed") from e

    def finish(self) -> None:
        try:
            self._parser.finalize()
        except MultipartParseError as e:
            raise UploadRefused(400, "the multipart body is malformed") from e
        if not self._ended:
            raise UploadRefused(400, "the multipart body was cut off")

    def _count(self, n: int) -> None:
        self._header_bytes += n
        if self._header_bytes > _MAX_PART_HEADER_BYTES:
            raise UploadRefused(400, "the part headers are too large")

    def _part_begin(self) -> None:
        self._headers = {}
        self._header_bytes = 0

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._count(end - start)
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._count(end - start)
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        raw = options.get(b"filename")
        if raw is None:
            raise UploadRefused(400, "every part must be a file")
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise UploadRefused(400, "a file name must be one plain name") from e
        self._session.begin_file(name)

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        self._session.write(data[start:end])

    def _end(self) -> None:
        self._ended = True
