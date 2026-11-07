"""Mail (enterprise-security.md item 6). Locally the outbox is a private file; hosted it is
a provider set by env.

  IRONWRIGHT_MAIL       file (default) or resend
  IRONWRIGHT_MAIL_KEY   the provider's API key
  IRONWRIGHT_MAIL_FROM  the From line

The file is <data root>/outbox/mail.jsonl, mode 0600, one message per line. It holds
live links, which is the point: it is the local inbox. A message is sent after the
transaction that made its link commits, so a mail never names a link that was rolled
back. Links are built from IRONWRIGHT_PUBLIC_URL and the base path, never from the
request's Host header, which the client writes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

from ..config import bind_host, bind_port, private_dir, secure_data_root, setting
from . import cookies

log = logging.getLogger("ironwright.mail")
RESEND_URL = "https://api.resend.com/emails"
_transport: httpx.BaseTransport | None = None  # tests swap in a MockTransport


class MailError(RuntimeError):
    pass


def file_path() -> Path:
    return secure_data_root() / "outbox" / "mail.jsonl"


def link(fragment: str) -> str:
    """A console link. The token rides in the fragment, which no server or log sees."""
    origin = (setting("PUBLIC_URL") or f"http://{bind_host()}:{bind_port()}").rstrip("/")
    base = cookies.base_path().rstrip("/")
    return f"{origin}{base}/console/#{fragment}"


def _to_file(to: str, subject: str, text: str) -> None:
    folder = private_dir(file_path().parent)
    fd = os.open(folder / "mail.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "to": to, "subject": subject, "text": text}) + "\n")


def _to_resend(to: str, subject: str, text: str) -> None:
    key, sender = setting("MAIL_KEY"), setting("MAIL_FROM")
    if not key or not sender:
        raise MailError("IRONWRIGHT_MAIL=resend needs IRONWRIGHT_MAIL_KEY and IRONWRIGHT_MAIL_FROM")
    with httpx.Client(transport=_transport, timeout=10) as client:
        r = client.post(RESEND_URL, headers={"authorization": f"Bearer {key}"},
                        json={"from": sender, "to": [to], "subject": subject, "text": text})
    if r.status_code >= 300:
        raise MailError(f"the mail provider answered {r.status_code}")


def forget(terms: list[str]) -> None:
    """Drop every local mailbox line that holds one of these terms. For an org delete."""
    path = file_path()
    terms = [t for t in terms if t]
    if not terms or not path.is_file() or path.is_symlink():
        return
    kept = [line for line in path.read_text().splitlines(keepends=True)
            if not any(t in line or json.dumps(t)[1:-1] in line for t in terms)]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.writelines(kept)
    os.replace(tmp, path)


SINKS = {"file": _to_file, "resend": _to_resend}


def send(to: str, subject: str, text: str) -> None:
    kind = (setting("MAIL") or "file").strip().lower()
    sink = SINKS.get(kind)
    if sink is None:
        raise MailError(f"IRONWRIGHT_MAIL must be one of {', '.join(SINKS)}, not {kind}")
    sink(to, subject, text)


def _send_all(messages: list[tuple[str, str, str]]) -> None:
    for to, subject, text in messages:
        try:
            send(to, subject, text)
        except (MailError, httpx.HTTPError) as e:
            log.error("mail not sent: %s", e)


def deliver(messages: list[tuple[str, str, str]]) -> None:
    """After commit. A provider failure is logged and never answered: a reset request for
    an email must look the same whether or not a mail went out. So must its timing, so a
    provider is called off the request thread, and the answer never waits on it. The
    local file is written at once, which keeps the local inbox in order."""
    if (setting("MAIL") or "file").strip().lower() == "file":
        _send_all(messages)
    elif messages:
        threading.Thread(target=_send_all, args=(messages,), daemon=True, name="mail").start()
