"""Bounded HTML-to-text and link extraction using the standard parser."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .security import UnsafeEgressTarget, normalize_http_url

_SPACE = re.compile(r"\s+")
_SKIP = frozenset({"script", "style", "noscript", "template", "svg"})


def bounded_text(value: object, limit: int) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    return text[:limit]


class _Extractor(HTMLParser):
    def __init__(self, *, base_url: str, max_text_chars: int, max_links: int, max_url_chars: int):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_text_chars = max_text_chars
        self.max_links = max_links
        self.max_url_chars = max_url_chars
        self.skip_depth = 0
        self.in_title = False
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._seen_links: set[str] = set()
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self._text_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag == "a" and len(self.links) < self.max_links:
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            if href:
                self._active_href = href
                self._active_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._active_href is not None:
            try:
                absolute = normalize_http_url(
                    urljoin(self.base_url, self._active_href),
                    max_chars=self.max_url_chars,
                )
            except UnsafeEgressTarget:
                absolute = ""
            if absolute and absolute not in self._seen_links and len(self.links) < self.max_links:
                self._seen_links.add(absolute)
                self.links.append(
                    {
                        "url": absolute,
                        "text": bounded_text(" ".join(self._active_text), 300),
                    }
                )
            self._active_href = None
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data:
            return
        clean = bounded_text(data, self.max_text_chars)
        if not clean:
            return
        if self.in_title and sum(len(part) for part in self.title_parts) < 500:
            self.title_parts.append(clean)
        if self._active_href is not None and sum(len(part) for part in self._active_text) < 300:
            self._active_text.append(clean)
        if self._text_chars < self.max_text_chars:
            remaining = self.max_text_chars - self._text_chars
            chunk = clean[:remaining]
            self.text_parts.append(chunk)
            self._text_chars += len(chunk) + 1


def extract_html(
    html: str,
    *,
    base_url: str,
    max_text_chars: int,
    max_links: int,
    max_url_chars: int,
) -> tuple[str, str, list[dict[str, str]]]:
    parser = _Extractor(
        base_url=base_url,
        max_text_chars=max_text_chars,
        max_links=max_links,
        max_url_chars=max_url_chars,
    )
    parser.feed(html[: max_text_chars * 8])
    parser.close()
    title = bounded_text(" ".join(parser.title_parts), 500)
    text = bounded_text(" ".join(parser.text_parts), max_text_chars)
    return title, text, parser.links
