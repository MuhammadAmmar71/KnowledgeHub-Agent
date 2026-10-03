"""
Spec section 30: remove scripts, styles, navigation noise, irrelevant
boilerplate; preserve headings, paragraphs, meaningful links, lists,
tables. Uses the stdlib html.parser rather than pulling in a third-party
HTML library, since a lightweight structural walk is all extraction needs
here (no rendering, no CSS).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile

_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "br", "section", "article", "header", "footer", "table", "ul", "ol",
}
_SKIP_TAGS = {"script", "style", "nav", "noscript", "head"}

_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._pending_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            self._pending_href = dict(attrs).get("href")
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._pending_href:
            self._parts.append(f" ({self._pending_href})")
            self._pending_href = None
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = _MULTI_SPACE_RE.sub(" ", raw)
        raw = _MULTI_NEWLINE_RE.sub("\n\n", raw)
        return raw.strip()


class HtmlProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        raw_html = file.content.decode("utf-8", errors="replace")
        parser = _HtmlTextExtractor()
        try:
            parser.feed(raw_html)
        except Exception as exc:
            return ExtractedContent(text="", warnings=[f"HTML parsing failed: {exc}"])
        return ExtractedContent(text=parser.get_text())
