"""Non-executing DOCX and spreadsheet extraction."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .base import Extraction, ExtractionError, Fragment, registry, snapshot_file


def _extract_docx_snapshot(path: Path) -> Extraction:
    try:
        document = Document(path)
    except Exception as exc:
        raise ExtractionError(f"failed to read DOCX file: {path}") from exc
    return Extraction(
        "extracted",
        [
            Fragment(f"paragraph:{index}", paragraph.text)
            for index, paragraph in enumerate(document.paragraphs, 1)
            if paragraph.text.strip()
        ],
    )


def extract_docx(path: Path) -> Extraction:
    with snapshot_file(path) as snapshot:
        return _extract_docx_snapshot(snapshot)


def _extract_xlsx_snapshot(path: Path) -> Extraction:
    try:
        book = load_workbook(
            path,
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except Exception as exc:
        raise ExtractionError(f"failed to read spreadsheet file: {path}") from exc

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
        raise ExtractionError(f"failed to extract spreadsheet file: {path}") from exc
    finally:
        book.close()


def extract_xlsx(path: Path) -> Extraction:
    with snapshot_file(path) as snapshot:
        return _extract_xlsx_snapshot(snapshot)


class DocxExtractor:
    suffixes = {".docx"}
    extract = staticmethod(extract_docx)
    extract_snapshot = staticmethod(_extract_docx_snapshot)


class XlsxExtractor:
    suffixes = {".xlsx", ".xlsm"}
    extract = staticmethod(extract_xlsx)
    extract_snapshot = staticmethod(_extract_xlsx_snapshot)


registry.register(DocxExtractor())
registry.register(XlsxExtractor())
