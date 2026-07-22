"""Small, deterministic byte-signature MIME classifier.

This is intentionally conservative. Unknown or ambiguous inputs remain
``application/octet-stream`` and are decided by the scanner policy.
"""
from __future__ import annotations

SNIFF_BYTES = 8192


def normalize_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def sniff_mime(prefix: bytes) -> str:
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    if prefix.startswith(b"\x1f\x8b"):
        return "application/gzip"
    stripped = prefix.lstrip()
    if stripped.startswith((b"{", b"[")):
        try:
            stripped.decode("utf-8")
            return "application/json"
        except UnicodeDecodeError:
            pass
    if b"\x00" not in prefix:
        try:
            prefix.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream"


def declared_mime_matches(declared: str, actual: str) -> bool:
    declared = normalize_content_type(declared)
    actual = normalize_content_type(actual)
    if declared == "application/octet-stream":
        return True
    aliases = {
        "image/jpg": "image/jpeg",
        "text/json": "application/json",
        "application/x-zip-compressed": "application/zip",
    }
    zip_containers = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/java-archive",
    }
    text_containers = {
        "text/csv",
        "text/markdown",
    }
    if actual == "application/zip" and declared in zip_containers:
        return True
    # The bounded sniffer deliberately classifies generic UTF-8 bytes as
    # text/plain. Markdown and CSV have no reliable byte signature, so retain
    # their declaration while treating a plain-text sniff as compatible.
    if actual == "text/plain" and declared in text_containers:
        return True
    return aliases.get(declared, declared) == aliases.get(actual, actual)
