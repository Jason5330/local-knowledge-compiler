"""Non-executing DOCX and spreadsheet extraction."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .base import Extraction, ExtractionError, Fragment, registry, require_regular_file


def extract_docx(path: Path) -> Extraction:
    candidate = require_regular_file(path)
    try:
        document = Document(candidate)
    except Exception as exc:
        raise ExtractionError(f"failed to read DOCX file: {candidate}") from exc
    return Extraction(
        "extracted",
        [
            Fragment(f"paragraph:{index}", paragraph.text)
            for index, paragraph in enumerate(document.paragraphs, 1)
            if paragraph.text.strip()
        ],
    )


def extract_xlsx(path: Path) -> Extraction:
    candidate = require_regular_file(path)
    try:
        book = load_workbook(
            candidate,
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except Exception as exc:
        raise ExtractionError(f"failed to read spreadsheet file: {candidate}") from exc

    try:
        fragments: list[Fragment] = []
        for sheet in book.worksheets:
            for row_number, row in enumerate(sheet.iter_rows(), 1):
                values = ["" if cell.value is None else str(cell.value) for cell in row]
                if any(values):
                    locator = (
                        f"sheet:{sheet.title};cells:A{row_number}-"
                        f"{get_column_letter(len(row))}{row_number}"
                    )
                    fragments.append(Fragment(locator, "\t".join(values)))
        return Extraction("extracted", fragments)
    except Exception as exc:
        raise ExtractionError(f"failed to extract spreadsheet file: {candidate}") from exc
    finally:
        book.close()


class DocxExtractor:
    suffixes = {".docx"}
    extract = staticmethod(extract_docx)


class XlsxExtractor:
    suffixes = {".xlsx", ".xlsm"}
    extract = staticmethod(extract_xlsx)


registry.register(DocxExtractor())
registry.register(XlsxExtractor())
