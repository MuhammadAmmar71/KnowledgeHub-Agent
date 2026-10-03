"""
Spec section 30: parse the diagram XML, extract node labels and
relationship info, extract diagram/page names. Don't treat raw XML as the
final representation.

Draw.io files store each <diagram> either as plain nested XML, or (more
commonly, when saved from the draw.io app) as base64 + raw-deflate +
URL-encoded text content. Both forms are handled here since either can
show up depending on how the file was created/saved.
"""
from __future__ import annotations

import base64
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_label(value: str) -> str:
    # mxCell "value" attributes can themselves contain HTML-escaped inline
    # markup (e.g. <br> for line breaks within a shape's label).
    text = _TAG_RE.sub(" ", value)
    return html.unescape(text).strip()


def _decode_compressed_diagram(text_content: str) -> ET.Element:
    compressed = base64.b64decode(text_content)
    decompressed = zlib.decompress(compressed, -15)  # raw deflate, no zlib header
    decoded = urllib.parse.unquote(decompressed.decode("utf-8"))
    return ET.fromstring(decoded)


class DrawioProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        raw_xml = file.content.decode("utf-8", errors="replace")
        warnings: list[str] = []

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            return ExtractedContent(text="", warnings=[f"failed to parse .drawio XML: {exc}"])

        diagram_sections: list[str] = []

        for diagram_el in root.iter("diagram"):
            page_name = diagram_el.get("name", "")

            if len(list(diagram_el)) > 0:
                # Uncompressed form: mxGraphModel nested directly.
                inner_root = diagram_el
            else:
                text_content = (diagram_el.text or "").strip()
                if not text_content:
                    continue
                try:
                    inner_root = _decode_compressed_diagram(text_content)
                except Exception as exc:
                    warnings.append(f"failed to decode compressed diagram page '{page_name}': {exc}")
                    continue

            labels = [
                _clean_label(value)
                for cell in inner_root.iter("mxCell")
                if (value := cell.get("value")) and _clean_label(value)
            ]

            if labels:
                diagram_sections.append(f"PAGE: {page_name}\n" + "\n".join(labels))

        text = "\n\n".join(diagram_sections)
        return ExtractedContent(text=text, warnings=warnings, metadata={"page_count": len(diagram_sections)})
