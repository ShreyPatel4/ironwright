"""The narration guardrail. Model text and templates both pass through it.

Rules decide, the LLM narrates, and a narration can only say what was gathered:

- A citation to an id not gathered in this run is removed. The caller decides
  if that costs the narration its grounding.
- Every link goes, whatever the shape. I do not exempt a URL because it came
  from evidence: the planted link IS in evidence, so a provenance check would
  wave exactly that one through. The row itself still holds it as data.
- A sentence that reads as an instruction or a fake approval goes too, on the
  same reasoning. This is pattern matching, so it is a second line and not the
  proof. The proof is that no decision path reads a narration at all.
- Text that carries our own fence was the prompt coming back, not narration.

Templates go through this as well: they interpolate cell values, so a cell can
plant a citation in a template with no model anywhere near it.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..llm import sanitize

LINK_REMOVED = "[link removed]"
REMOVED = "[removed]"

_CITE = re.compile(r"\[\s*ev\s*:\s*([^\]]*)\]", re.IGNORECASE)
_ECHO = re.compile(r"</?\s*untrusted[-\s>]", re.IGNORECASE)
_TLDS = "com|net|org|io|co|ai|ru|cn|xyz|info|biz|app|dev|link|click|top|site|online|example"
_LINKS = [
    re.compile(r"!?\[([^\]]*)\]\([^)]*\)"),                      # [label](url), ![alt](url): keep the label
    re.compile(r"!?\[([^\]]*)\]\[[^\]]*\]"),                     # [label][ref]
]
_LINK_REMOVALS = [
    re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE),               # [ref]: url
    re.compile(r"<a\b[^>]*>.*?</a\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<[a-z]+\b[^>]*\b(?:href|src)\s*=[^>]*>", re.IGNORECASE),
    re.compile(r"<[a-z][a-z0-9+.-]*:[^>\s]*>", re.IGNORECASE),            # <https://...>
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\]]+", re.IGNORECASE),
    re.compile(r"\b(?:javascript|vbscript|mailto|file):[^\s\]]+", re.IGNORECASE),
    re.compile(r"\bdata:[a-z]+/[^\s\]]+", re.IGNORECASE),
    re.compile(r"(?<![\w/])//[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s\]]*", re.IGNORECASE),
    re.compile(r"\bwww\.[^\s\]]+", re.IGNORECASE),
    re.compile(rf"\b(?:[a-z0-9-]+\.)+(?:{_TLDS})\b(?:/[^\s\]]*)?", re.IGNORECASE),
]
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:\w+\s+){0,3}?(?:instructions?|prompts?|rules?|guardrails?|directions?)\b"
    r"|\byou\s+are\s+now\b|\bact\s+as\s+(?:an?|the)\b|\bpretend\s+to\s+be\b|\bnew\s+instructions?\b"
    r"|\bsystem\s+prompt\b|^\W*(?:system|assistant|developer)\s*:"
    r"|\b(?:call|invoke|execute|use|run)\s+(?:the\s+|this\s+)?(?:tool|function|action)\b"
    r"|\b[a-z]+_[a-z0-9_]+\s*\([^)]*=[^)]*\)"
    r"|\b(?:approved|authori[sz]ed|signed\s+off)\s+by\b|\bapproval\s+(?:granted|given|confirmed)\b"
    r"|[\"']?\bapprov(?:ed|al)(?:_by)?[\"']?\s*[:=]\s*[\"']?(?:true|yes|granted|[\w.:-]+@)"
    r"|\bauto-?approve\b"
    r"|\b(?:approve|pay|wire|transfer|release|delete|drop)\b[^.!?\n]*\b(?:immediately|right\s+now|without\s+(?:review|approval))\b",
    re.IGNORECASE | re.MULTILINE)
_SENTENCE = re.compile(r"((?<=[.!?])\s+|\n)")


@dataclass
class Guarded:
    text: str
    cited: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    echoed: bool = False

    @property
    def clean(self) -> bool:
        return not (self.unknown or self.dropped or self.echoed)

    def grounds(self, required: Iterable[str]) -> bool:
        """Non-empty, every required citation present, nothing invented."""
        return bool(self.text) and not self.unknown and not self.echoed and set(required) <= set(self.cited)

    def reasons(self) -> list[str]:
        out = ["unknown_citation"] if self.unknown else []
        return out + (["echoed_prompt"] if self.echoed else []) + self.dropped


def _drop_links(text: str) -> tuple[str, bool]:
    before = text
    for pat in _LINKS:
        text = pat.sub(lambda m: m.group(1), text)
    for pat in _LINK_REMOVALS:
        text = pat.sub(LINK_REMOVED, text)
    return text, text != before


def _drop_instructions(text: str) -> tuple[str, bool]:
    parts = _SENTENCE.split(text)
    kept: list[str] = []
    hit = False
    for i in range(0, len(parts), 2):
        if _INSTRUCTION.search(parts[i]):
            hit = True
            continue
        if kept:
            kept.append(parts[i - 1])
        kept.append(parts[i])
    return ("".join(kept), hit) if hit else (text, False)


def guard(text: str, known: Iterable[str]) -> Guarded:
    known = set(known)
    text = unicodedata.normalize("NFKC", sanitize(text))
    g = Guarded(text=text, echoed=bool(_ECHO.search(text)))
    text, hit = _drop_links(text)
    if hit:
        g.dropped.append("link")

    def cite(m: re.Match) -> str:
        cid = m.group(1).strip()
        if cid in known:
            if cid not in g.cited:
                g.cited.append(cid)
            return f"[ev:{cid}]"
        g.unknown.append(cid)
        return ""

    text = _CITE.sub(cite, text)
    text, hit = _drop_instructions(text)
    if hit:
        g.dropped.append("instruction")
        g.cited = [c for c in g.cited if f"[ev:{c}]" in text]
    if not g.clean:
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    g.text = text
    return g


def scrub_value(value: object) -> str:
    """One cell for a template, scrubbed on its own so the authored sentence
    and the honest fields beside it still show."""
    g = guard(str(value), ())
    return g.text if g.clean or g.text else REMOVED


def settle(raw: str | None, template: str, known: Iterable[str],
           required: Iterable[str] = ()) -> tuple[str, bool, list[str], list[str]]:
    """The one exit for narration: (text, grounded, reasons, cited). Model text
    survives only if it still grounds after the guard; otherwise the template
    stands in, itself guarded, and the narration is marked not grounded."""
    known = set(known)
    fallback = guard(template, known)
    missing = [c for c in required if c in known and c not in fallback.cited]
    if missing:  # the rule's citations are ours; a dropped sentence must not take them along
        fallback.text = " ".join([fallback.text, *(f"[ev:{c}]" for c in missing)]).strip()
        fallback.cited += missing
    if raw is None:
        return fallback.text, True, [], fallback.cited
    g = guard(raw, known)
    if g.grounds(required):
        return g.text, True, g.reasons(), g.cited
    return fallback.text, False, g.reasons() or ["missing_citation"], fallback.cited
