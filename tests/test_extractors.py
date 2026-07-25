from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from docx import Document
from openpyxl import Workbook

from local_kb.extractors.base import Extraction, Fragment, Registry, registry


def test_text_extractor_preserves_chinese_nonempty_line_locators(tmp_path: Path) -> None:
    path = tmp_path / "筆記.MD"
    path.write_text("第一行\n\n繁體中文內容\n", encoding="utf-8")

    result = registry.extract(path)

    assert result.status == "extracted"
    assert result.fragments == [
        Fragment("lines:1-1", "第一行"),
        Fragment("lines:3-3", "繁體中文內容"),
    ]


def test_unknown_media_is_pending_not_fabricated(tmp_path: Path) -> None:
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"not-a-real-video")

    result = registry.extract(path)

    assert result.status == "pending_extractor"
    assert result.fragments == []
    assert result.warning == "no extractor for .mp4"


def test_suffix_matching_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "UPPER.TXT"
    path.write_text("ok", encoding="utf-8")

    assert registry.extract(path).fragments == [Fragment("lines:1-1", "ok")]


def test_file_without_suffix_is_pending_with_clear_warning(tmp_path: Path) -> None:
    path = tmp_path / "README"
    path.write_text("untyped", encoding="utf-8")

    result = registry.extract(path)

    assert result.status == "pending_extractor"
    assert result.fragments == []
    assert result.warning == "no extractor for no suffix"


def test_html_strips_active_and_navigation_content_but_keeps_title_and_body(tmp_path: Path) -> None:
    path = tmp_path / "guide.html"
    path.write_text(
        "<html><head><title>指南</title><style>secret-style</style></head>"
        "<body><nav>secret-nav</nav><script>secret-script</script><p>正文</p></body></html>",
        encoding="utf-8",
    )

    result = registry.extract(path)

    assert result.status == "extracted"
    assert result.fragments == [Fragment("title:指南", "指南\n正文")]


def test_html_title_falls_back_to_filename_stem(tmp_path: Path) -> None:
    path = tmp_path / "untitled.htm"
    path.write_text("<p>body</p>", encoding="utf-8")

    assert registry.extract(path).fragments == [Fragment("title:untitled", "body")]


def test_pdf_with_text_creates_page_locators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from local_kb.extractors import pdf

    path = tmp_path / "report.pdf"
    path.write_bytes(b"placeholder")

    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class Reader:
        pages = [Page("首頁"), Page(""), Page("第二頁")]

    monkeypatch.setattr(pdf, "PdfReader", lambda _: Reader())

    assert registry.extract(path).fragments == [
        Fragment("page:1", "首頁"),
        Fragment("page:3", "第二頁"),
    ]


def test_blank_pdf_is_pending_for_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from local_kb.extractors import pdf

    path = tmp_path / "scan.pdf"
    path.write_bytes(b"placeholder")

    class Page:
        def extract_text(self) -> str:
            return "  "

    class Reader:
        pages = [Page()]

    monkeypatch.setattr(pdf, "PdfReader", lambda _: Reader())

    result = registry.extract(path)

    assert result.status == "pending_extractor"
    assert result.fragments == []
    assert result.warning == "PDF has no extractable text; OCR required"


def test_docx_uses_only_nonempty_paragraphs_with_paragraph_locators(tmp_path: Path) -> None:
    path = tmp_path / "memo.docx"
    document = Document()
    document.add_paragraph("第一段")
    document.add_paragraph("  ")
    document.add_paragraph("第三段")
    document.save(path)

    assert registry.extract(path).fragments == [
        Fragment("paragraph:1", "第一段"),
        Fragment("paragraph:3", "第三段"),
    ]


def test_xlsx_extracts_each_nonempty_row_across_sheets_and_keeps_none_cells(tmp_path: Path) -> None:
    path = tmp_path / "table.xlsx"
    book = Workbook()
    first = book.active
    first.title = "摘要"
    first.append(["名稱", None, 3])
    first.append([None, None, None])
    second = book.create_sheet("資料")
    second.append([None, "值"])
    book.save(path)
    book.close()

    assert registry.extract(path).fragments == [
        Fragment("sheet:摘要;cells:A1-C1", "名稱\t\t3"),
        Fragment("sheet:資料;cells:A1-B1", "\t值"),
    ]


def test_xlsx_workbook_is_closed_after_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from local_kb.extractors import office

    path = tmp_path / "table.xlsx"
    path.write_bytes(b"placeholder")

    class Cell:
        def __init__(self, coordinate: str, value: object) -> None:
            self.coordinate = coordinate
            self.value = value

    class Sheet:
        title = "Data"

        def iter_rows(self):
            yield (Cell("A1", "value"),)

    class Book:
        worksheets = [Sheet()]
        closed = False

        def close(self) -> None:
            self.closed = True

    book = Book()
    monkeypatch.setattr(office, "load_workbook", lambda *args, **kwargs: book)

    result = registry.extract(path)

    assert result.fragments == [Fragment("sheet:Data;cells:A1-A1", "value")]
    assert book.closed is True


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx"])
def test_corrupt_supported_documents_never_claim_extracted(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"corrupt{suffix}"
    path.write_bytes(b"not a document")

    with pytest.raises(Exception):
        registry.extract(path)


def test_registry_rejects_duplicate_suffixes() -> None:
    class One:
        suffixes = {".one"}

        def extract(self, path: Path) -> Extraction:
            return Extraction("extracted", [])

    class Two:
        suffixes = {".ONE"}

        def extract(self, path: Path) -> Extraction:
            return Extraction("extracted", [])

    isolated = Registry()
    isolated.register(One())

    with pytest.raises(ValueError, match="duplicate extractor suffix: .one"):
        isolated.register(Two())


def test_extractions_do_not_share_mutable_fragment_lists() -> None:
    first = Extraction("extracted", [])
    second = Extraction("extracted", [])

    first.fragments.append(Fragment("lines:1-1", "only first"))

    assert second.fragments == []


def test_registry_rejects_directories_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        registry.extract(tmp_path)

    target = tmp_path / "target.txt"
    target.write_text("not read by link", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(ValueError, match="symbolic link"):
        registry.extract(link)


def test_registry_rejects_a_file_below_a_parent_junction(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows-only filesystem feature")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be read", encoding="utf-8")
    junction = tmp_path / "linked-parent"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink /J linked-parent outside"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr

    with pytest.raises(ValueError, match="reparse"):
        registry.extract(junction / "secret.txt")


def test_registry_extracts_a_stable_snapshot_not_a_path_reopened_after_dispatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mutable.snap"
    source.write_text("before", encoding="utf-8")

    class ReplacingExtractor:
        suffixes = {".snap"}

        def extract(self, path: Path) -> Extraction:
            source.write_text("after", encoding="utf-8")
            return Extraction("extracted", [Fragment("snapshot", path.read_text(encoding="utf-8"))])

    isolated = Registry()
    isolated.register(ReplacingExtractor())

    result = isolated.extract(source)

    assert result.fragments == [Fragment("snapshot", "before")]
    assert source.read_text(encoding="utf-8") == "after"


def test_parent_replacement_after_snapshot_cannot_change_parser_input(tmp_path: Path) -> None:
    parent = tmp_path / "source-parent"
    parent.mkdir()
    source = parent / "race.snap"
    source.write_text("before", encoding="utf-8")
    replacement = tmp_path / "replaced-parent"
    rename_failed = False

    class ParentReplacingExtractor:
        suffixes = {".snap"}

        def extract(self, path: Path) -> Extraction:
            nonlocal rename_failed
            try:
                parent.rename(replacement)
                parent.mkdir()
                (parent / "race.snap").write_text("outside", encoding="utf-8")
            except OSError:
                rename_failed = True
            return Extraction("extracted", [Fragment("snapshot", path.read_text(encoding="utf-8"))])

    isolated = Registry()
    isolated.register(ParentReplacingExtractor())

    result = isolated.extract(source)

    assert result.fragments == [Fragment("snapshot", "before")]
    if os.name == "nt":
        assert rename_failed is True


def test_registry_removes_snapshot_after_success_and_parser_failure(tmp_path: Path) -> None:
    source = tmp_path / "temporary.snap"
    source.write_text("content", encoding="utf-8")
    seen_paths: list[Path] = []

    class CapturingExtractor:
        suffixes = {".snap"}

        def extract(self, path: Path) -> Extraction:
            seen_paths.append(path)
            assert path.exists()
            return Extraction("extracted", [])

    isolated = Registry()
    isolated.register(CapturingExtractor())
    isolated.extract(source)
    assert not seen_paths[0].exists()
    assert not seen_paths[0].parent.exists()

    class FailingExtractor:
        suffixes = {".fail"}

        def extract(self, path: Path) -> Extraction:
            seen_paths.append(path)
            raise RuntimeError("parser failed")

    failing_source = tmp_path / "temporary.fail"
    failing_source.write_text("content", encoding="utf-8")
    failing = Registry()
    failing.register(FailingExtractor())
    with pytest.raises(RuntimeError, match="parser failed"):
        failing.extract(failing_source)
    assert not seen_paths[1].exists()
    assert not seen_paths[1].parent.exists()
