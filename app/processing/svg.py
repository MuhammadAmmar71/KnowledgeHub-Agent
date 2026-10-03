"""
SVG diagrams: like .drawio, the raw XML is not a useful representation --
path data and styling carry no searchable meaning. What does carry meaning
is the human-readable text: the document <title>/<desc>, and the labels in
<text> elements (including nested <tspan> lines). Those are extracted in
document order; everything else is dropped.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile

_WHITESPACE_RE = re.compile(r"\s+")


def _local_name(tag: str) -> str:
    # ElementTree reports namespaced tags as "{http://www.w3.org/2000/svg}text".
    return tag.rsplit("}", 1)[-1]


def _element_text(element: ET.Element) -> str:
    return _WHITESPACE_RE.sub(" ", "".join(element.itertext())).strip()


class SvgProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        raw_xml = file.content.decode("utf-8", errors="replace")

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            return ExtractedContent(text="", warnings=[f"failed to parse .svg XML: {exc}"])

        title = ""
        description = ""
        labels: list[str] = []

        for element in root.iter():
            name = _local_name(element.tag)
            if name == "title" and not title:
                title = _element_text(element)
            elif name == "desc" and not description:
                description = _element_text(element)
            elif name == "text":
                # itertext() already covers nested <tspan>s, so they are
                # never visited as separate labels.
                label = _element_text(element)
                if label:
                    labels.append(label)

        parts = [p for p in (title, description) if p]
        parts.extend(labels)

        warnings: list[str] = []
        if not parts:
            warnings.append("SVG contains no text labels (shapes/paths only)")

        metadata = {"title": title} if title else {}
        return ExtractedContent(text="\n".join(parts), metadata=metadata, warnings=warnings)
