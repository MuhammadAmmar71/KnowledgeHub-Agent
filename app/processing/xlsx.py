"""
Spec section 30: extract workbook name, sheet name, meaningful cell
values, table-like structures; avoid producing an enormous meaningless
flattened string (hence the per-sheet row cap).
"""
from __future__ import annotations

import io

import openpyxl

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile

_MAX_ROWS_PER_SHEET = 200


class XlsxProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(file.content), data_only=True, read_only=True)
        except Exception as exc:
            return ExtractedContent(text="", warnings=[f"failed to open .xlsx: {exc}"])

        parts: list[str] = [f"WORKBOOK: {file.filename}"]
        warnings: list[str] = []

        for sheet in workbook.worksheets:
            parts.append(f"\nSHEET: {sheet.title}")
            row_count = 0
            for row in sheet.iter_rows(values_only=True):
                if row_count >= _MAX_ROWS_PER_SHEET:
                    warnings.append(f"sheet '{sheet.title}' truncated at {_MAX_ROWS_PER_SHEET} rows")
                    break
                if all(cell is None for cell in row):
                    continue
                cells = ["" if c is None else str(c) for c in row]
                parts.append(" | ".join(cells))
                row_count += 1

        return ExtractedContent(text="\n".join(parts), warnings=warnings)
