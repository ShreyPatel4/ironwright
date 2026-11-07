"""The password policy (enterprise-security.md item 4): at least 12 characters and not on
the breached list. No composition rules, which only push people to Password1!.

The list is the top 100k from the NCSC's analysis of breached passwords, as published
in SecLists (Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt, MIT
licence). It ships gzipped in the wheel and is read once, on first use. A check is
case blind, so QWERTY123456 is as breached as qwerty123456.
"""
from __future__ import annotations

import gzip
from functools import cache
from importlib import resources

MIN_LENGTH = 12
MAX_LENGTH = 1024  # hashing a megabyte is a cheap way to burn our CPU
LIST_FILE = "breached-100k.txt.gz"


class Weak(ValueError):
    pass


@cache
def _breached() -> frozenset[str]:
    raw = resources.files(__package__).joinpath(LIST_FILE).read_bytes()
    return frozenset(w.lower() for w in gzip.decompress(raw).decode("utf-8").splitlines() if w)


def breached_count() -> int:
    return len(_breached())


def is_breached(password: str) -> bool:
    return password.lower() in _breached()


def check(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise Weak(f"a password needs at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise Weak(f"a password can have at most {MAX_LENGTH} characters")
    if is_breached(password):
        raise Weak("that password is on a list of breached passwords; pick another")
