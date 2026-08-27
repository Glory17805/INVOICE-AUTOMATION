"""What a file actually is, as opposed to what it is named.

Gap S6. Intake accepted a file on its extension alone, so `invoice.pdf` was
whatever the sender put in it. That matters here for two reasons that have
nothing to do with anyone being malicious: a mislabelled file reaches a parser
that cannot read it and fails confusingly, and an upload endpoint that trusts a
name is the first thing anyone probes on a system holding a company's tax
records.

So the bytes decide. The declared extension still has to agree with them -
a PNG named `.pdf` is refused rather than quietly re-typed, because the name a
person gave a file is information too, and a disagreement between the two is
worth surfacing rather than resolving silently.
"""

from __future__ import annotations

from dataclasses import dataclass

# Formats the pipeline can actually read, with the signature that identifies
# each. Order matters only in that the longest signatures are checked first.
_SIGNATURES: list[tuple[str, bytes]] = [
    ("png", b"\x89PNG\r\n\x1a\n"),
    ("pdf", b"%PDF-"),
    ("gif", b"GIF87a"),
    ("gif", b"GIF89a"),
    ("jpeg", b"\xff\xd8\xff"),
]

# Which sniffed kinds each extension is allowed to be.
_ALLOWED: dict[str, set[str]] = {
    ".pdf": {"pdf"},
    ".png": {"png"},
    ".jpg": {"jpeg"},
    ".jpeg": {"jpeg"},
    ".gif": {"gif"},
    ".webp": {"webp"},
    ".txt": {"text"},
    ".csv": {"text"},
}

MAX_SNIFF = 4096


class RejectedUpload(ValueError):
    """The file is not what it claims to be, or is not something we can read."""


@dataclass(frozen=True)
class Sniffed:
    kind: str          # "pdf" | "png" | "jpeg" | "gif" | "webp" | "text" | "unknown"
    detail: str        # human-facing description, used in the rejection message


def sniff(data: bytes) -> Sniffed:
    """Identify a file from its leading bytes."""
    head = data[:MAX_SNIFF]
    if not head:
        return Sniffed("unknown", "the file is empty")

    for kind, signature in _SIGNATURES:
        if head.startswith(signature):
            return Sniffed(kind, kind.upper())

    # WEBP is a RIFF container: "RIFF" .... "WEBP".
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return Sniffed("webp", "WEBP")

    # Anything that decodes as UTF-8 and holds no NUL is treatable as text.
    # A NUL byte is the reliable tell for "this is binary pretending not to be".
    if b"\x00" in head:
        return Sniffed("unknown", "binary content of an unrecognised format")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("utf-16")
        except UnicodeDecodeError:
            return Sniffed("unknown", "content of an unrecognised format")
    return Sniffed("text", "plain text")


def verify(data: bytes, filename: str, suffix: str) -> Sniffed:
    """Check a file's bytes against the extension it arrived with.

    Raises RejectedUpload with a message naming both sides of the disagreement,
    so whoever sent it can tell what happened without opening the file.
    """
    allowed = _ALLOWED.get(suffix)
    if allowed is None:
        raise RejectedUpload(
            f"{filename}: {suffix or 'files with no extension'} is not a type this reads."
        )

    found = sniff(data)
    if found.kind == "unknown":
        raise RejectedUpload(
            f"{filename}: this is not a readable {suffix.lstrip('.').upper()} - "
            f"the file contains {found.detail}."
        )
    if found.kind not in allowed:
        raise RejectedUpload(
            f"{filename}: named {suffix} but the contents are {found.detail}. "
            f"Rename it or send the original."
        )
    return found
