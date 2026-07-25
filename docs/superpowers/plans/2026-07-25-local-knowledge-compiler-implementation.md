# Local Knowledge Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一套 Windows 本地知識迭代系統：資料放入收件匣後自動歸檔、抽取與編譯，Codex／Claude 每次提問都從同一份本地證據包回答並回寫知識。

**Architecture:** Python 程式管理不可變原始來源、SQLite FTS5 搜尋目錄、Markdown Wiki、磁碟工作佇列與單一 writer。背景 AI 編譯器透過可替換 adapter 呼叫 Claude CLI；Codex 與 Claude 的互動工作階段則共同使用 `kb prepare`／`kb finalize`，所以資料格式與查詢流程不綁模型。

**Tech Stack:** Python 3.13、SQLite FTS5、Git、pytest 9.1.1、pypdf 6.14.2、python-docx 1.2.0、openpyxl 3.1.5、Beautiful Soup 4.15.0、defusedxml 0.7.1。

---

## Scope and delivery order

本規格雖包含多個子系統，但它們共用同一個資料模型和 CLI。實作依下列四個可單獨驗收的里程碑交付：

1. **Foundation**：資料夾、設定、SQLite、不可變來源、Git。
2. **Ingestion**：抽取器、收件匣監看、佇列、Wiki 編譯。
3. **Retrieval**：FTS5、多路召回、證據包、finalize 回寫。
4. **Reliability**：並行鎖、復原、健康檢查、Codex／Claude 規則與端到端驗收。

圖片 OCR、影片與錄音轉錄不在第一版安裝清單；這些檔案會安全歸檔並標為 `pending_extractor`。這是設計規格核准的明確降級行為，不得假裝已讀取。

## File map

```text
pyproject.toml                         # 套件、依賴、kb CLI 入口
README.md                              # 初學者安裝與日常使用說明
src/local_kb/
├── __init__.py
├── cli.py                             # init/watch/ingest/prepare/finalize/lint/rebuild
├── config.py                          # TOML 設定與預設值
├── paths.py                           # 所有受管理路徑
├── models.py                          # SourceVersion/Job/Evidence/ChangeSet
├── catalog.py                         # SQLite schema、FTS、查詢與重建
├── source_store.py                    # 雜湊、去重、不可變版本歸檔
├── extractors/
│   ├── base.py                        # Extractor protocol 與 registry
│   ├── text.py                        # txt/md/json/csv/code
│   ├── html.py                        # 本地 HTML
│   ├── pdf.py                         # PDF 頁碼文字
│   ├── office.py                      # DOCX/XLSX
│   └── unsupported.py                 # 圖片、影音與未知類型
├── queue.py                           # 磁碟工作狀態、重試、write.lock
├── ingest.py                          # 收錄流程協調器
├── watcher.py                         # 無額外服務依賴的輪詢監看器
├── wiki.py                            # Wiki frontmatter、渲染與驗證
├── compiler.py                        # Claude／manual 編譯 adapter
├── transaction.py                     # staging、原子發布與 Git commit
├── search.py                          # FTS、別名、連結擴展與排序
├── query.py                           # prepare 證據包
├── finalize.py                        # 保存回答與建立回寫工作
└── health.py                          # lint、孤兒頁、索引重建
src/local_kb/templates/
├── KNOWLEDGE_PROTOCOL.md
├── AGENTS.md
└── CLAUDE.md
tests/
├── conftest.py
├── test_init.py
├── test_catalog.py
├── test_source_store.py
├── test_extractors.py
├── test_queue_ingest.py
├── test_wiki_transaction.py
├── test_compiler.py
├── test_prepare.py
├── test_finalize.py
├── test_health_concurrency.py
└── test_e2e.py
```

## Milestone 1 — Foundation

### Task 1: Package bootstrap and vault initializer

**Files:**
- Create: `pyproject.toml`
- Create: `src/local_kb/__init__.py`
- Create: `src/local_kb/config.py`
- Create: `src/local_kb/paths.py`
- Create: `src/local_kb/cli.py`
- Create: `tests/conftest.py`
- Create: `tests/test_init.py`

- [ ] **Step 1: Write the failing initialization test**

```python
# tests/test_init.py
from local_kb.cli import build_vault


def test_build_vault_creates_required_tree(tmp_path):
    build_vault(tmp_path)
    expected = {
        "00_inbox", "10_raw", "20_wiki", "30_answers",
        "40_index", "80_system", "90_logs", "99_trash", ".kb",
    }
    assert expected <= {p.name for p in tmp_path.iterdir()}
    assert (tmp_path / "80_system" / "config.toml").exists()
    assert (tmp_path / ".kb" / "queue").is_dir()
```

- [ ] **Step 2: Add package metadata and run the test to prove it fails**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "local-knowledge-compiler"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "beautifulsoup4==4.15.0",
  "defusedxml==0.7.1",
  "openpyxl==3.1.5",
  "pypdf==6.14.2",
  "python-docx==1.2.0",
]

[project.optional-dependencies]
dev = ["pytest==9.1.1"]

[project.scripts]
kb = "local_kb.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Run:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\pytest.exe tests/test_init.py -v
```

Expected: FAIL with `ModuleNotFoundError` or missing `build_vault`.

- [ ] **Step 3: Implement focused path and config types**

```python
# src/local_kb/paths.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VaultPaths:
    root: Path

    @property
    def inbox(self) -> Path: return self.root / "00_inbox"
    @property
    def raw(self) -> Path: return self.root / "10_raw"
    @property
    def wiki(self) -> Path: return self.root / "20_wiki"
    @property
    def answers(self) -> Path: return self.root / "30_answers"
    @property
    def index(self) -> Path: return self.root / "40_index"
    @property
    def system(self) -> Path: return self.root / "80_system"
    @property
    def logs(self) -> Path: return self.root / "90_logs"
    @property
    def trash(self) -> Path: return self.root / "99_trash"
    @property
    def runtime(self) -> Path: return self.root / ".kb"
```

```python
# src/local_kb/config.py
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    vault: Path
    compiler: str = "claude"
    poll_seconds: float = 2.0
    stable_seconds: float = 5.0
    max_retries: int = 3

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            vault=path.parent.parent.resolve(),
            compiler=data["compiler"]["provider"],
            poll_seconds=float(data["watcher"]["poll_seconds"]),
            stable_seconds=float(data["watcher"]["stable_seconds"]),
            max_retries=int(data["queue"]["max_retries"]),
        )
```

```python
# src/local_kb/cli.py
from pathlib import Path

from .paths import VaultPaths


CONFIG = """[compiler]
provider = "claude"

[watcher]
poll_seconds = 2.0
stable_seconds = 5.0

[queue]
max_retries = 3
"""


def build_vault(root: Path) -> None:
    p = VaultPaths(root.resolve())
    for folder in (
        p.inbox, p.raw, p.wiki, p.answers, p.index,
        p.system, p.logs, p.trash, p.runtime,
        p.runtime / "queue", p.runtime / "staging",
    ):
        folder.mkdir(parents=True, exist_ok=True)
    for space in ("personal", "work", "projects", "shared", "unclassified"):
        (p.raw / space).mkdir(exist_ok=True)
        (p.wiki / space).mkdir(exist_ok=True)
    (p.system / "config.toml").write_text(CONFIG, encoding="utf-8")
```

- [ ] **Step 4: Add the CLI `init` command and verify**

```python
# append to src/local_kb/cli.py
import argparse


def main() -> int:
    parser = argparse.ArgumentParser(prog="kb")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        build_vault(args.path)
        print(f"Initialized knowledge vault: {args.path.resolve()}")
    return 0
```

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_init.py -v
.\.venv\Scripts\kb.exe init .\sandbox-vault
```

Expected: test PASS and the CLI prints `Initialized knowledge vault`.

- [ ] **Step 5: Commit milestone slice**

```powershell
git add pyproject.toml src/local_kb tests/conftest.py tests/test_init.py
git commit -m "feat: initialize local knowledge vault"
```

### Task 2: Durable catalog and FTS5 index

**Files:**
- Create: `src/local_kb/models.py`
- Create: `src/local_kb/catalog.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Write failing catalog tests**

```python
# tests/test_catalog.py
from local_kb.catalog import Catalog
from local_kb.models import SourceVersion


def test_catalog_indexes_and_searches_source(tmp_path):
    db = Catalog(tmp_path / "catalog.sqlite")
    db.initialize()
    source = SourceVersion(
        source_id="src_abc", version_id="ver_abc", space="work",
        original_name="note.md",
        relative_path="10_raw/work/src_abc/ver_abc/note.md",
        sha256="abc", media_type="text/markdown", status="extracted",
    )
    db.upsert_source(source, [("lines:1-1", "卡帕西的持續累積知識庫")])
    hits = db.search("累積知識庫", spaces={"work"})
    assert [hit.version_id for hit in hits] == ["ver_abc"]
```

- [ ] **Step 2: Run the focused test**

Run: `.\.venv\Scripts\pytest.exe tests/test_catalog.py -v`  
Expected: FAIL because `Catalog` and `SourceVersion` do not exist.

- [ ] **Step 3: Implement shared models**

```python
# src/local_kb/models.py
from dataclasses import asdict, dataclass, field
from typing import Literal

Space = str
JobState = Literal[
    "discovered", "stable", "fingerprinted", "archived", "extracted",
    "compiled", "validated", "published", "retrying", "pending_attention",
]


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    version_id: str
    space: Space
    original_name: str
    relative_path: str
    sha256: str
    media_type: str
    status: str
    previous_version_id: str | None = None


@dataclass(frozen=True)
class SearchHit:
    version_id: str
    source_id: str
    space: str
    relative_path: str
    locator: str
    text: str
    score: float


@dataclass
class Job:
    job_id: str
    source_path: str
    state: JobState = "discovered"
    attempts: int = 0
    error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
```

- [ ] **Step 4: Implement schema, FTS and space filtering**

```python
# src/local_kb/catalog.py
from pathlib import Path
import sqlite3

from .models import SearchHit, SourceVersion


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources (
  version_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  space TEXT NOT NULL,
  original_name TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  media_type TEXT NOT NULL,
  status TEXT NOT NULL,
  previous_version_id TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
  version_id UNINDEXED, source_id UNINDEXED, relative_path UNINDEXED,
  locator UNINDEXED, body, tokenize='unicode61'
);
"""


class Catalog:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    def upsert_source(self, source: SourceVersion, fragments: list[tuple[str, str]]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source.version_id, source.source_id, source.space, source.original_name, source.relative_path,
                 source.sha256, source.media_type, source.status, source.previous_version_id),
            )
            con.execute("DELETE FROM source_fts WHERE version_id = ?", (source.version_id,))
            con.executemany(
                "INSERT INTO source_fts VALUES (?, ?, ?, ?, ?)",
                [(source.version_id, source.source_id, source.relative_path, locator, body)
                 for locator, body in fragments if body.strip()],
            )

    def latest_source(self, space: str, original_name: str) -> SourceVersion | None:
        with self.connect() as con:
            row = con.execute(
                """SELECT * FROM sources WHERE space = ? AND original_name = ?
                   ORDER BY rowid DESC LIMIT 1""",
                (space, original_name),
            ).fetchone()
        return SourceVersion(**dict(row)) if row else None

    def search(self, query: str, spaces: set[str], limit: int = 20) -> list[SearchHit]:
        marks = ",".join("?" for _ in spaces)
        sql = f"""
        SELECT f.*, s.space AS space, bm25(source_fts) AS rank
        FROM source_fts f JOIN sources s USING(version_id)
        WHERE source_fts MATCH ? AND s.space IN ({marks})
        ORDER BY rank LIMIT ?
        """
        with self.connect() as con:
            rows = con.execute(sql, (query, *sorted(spaces), limit)).fetchall()
        return [
            SearchHit(r["version_id"], r["source_id"], r["space"], r["relative_path"],
                      r["locator"], r["body"], float(-r["rank"]))
            for r in rows
        ]
```

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_catalog.py -v
git add src/local_kb/models.py src/local_kb/catalog.py tests/test_catalog.py
git commit -m "feat: add durable FTS catalog"
```

Expected: PASS.

### Task 3: Immutable source storage, deduplication, and version chains

**Files:**
- Create: `src/local_kb/source_store.py`
- Create: `tests/test_source_store.py`

- [ ] **Step 1: Write failing immutable storage tests**

```python
# tests/test_source_store.py
from local_kb.source_store import SourceStore


def test_same_bytes_deduplicate_and_changed_bytes_version(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    raw = tmp_path / "raw"
    store = SourceStore(raw)

    first = inbox / "policy.txt"
    first.write_text("版本一", encoding="utf-8")
    v1 = store.archive(first, "work")

    duplicate = inbox / "copy.txt"
    duplicate.write_text("版本一", encoding="utf-8")
    same = store.archive(duplicate, "work")
    assert same.version_id == v1.version_id

    changed = inbox / "policy.txt"
    changed.write_text("版本二", encoding="utf-8")
    v2 = store.archive(
        changed, "work", source_id=v1.source_id,
        previous_version_id=v1.version_id,
    )
    assert v2.previous_version_id == v1.version_id
    assert (raw / v1.relative_path.removeprefix("10_raw/")).exists()
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_source_store.py -v`  
Expected: FAIL because `SourceStore` is missing.

- [ ] **Step 3: Implement content-addressed archive**

```python
# src/local_kb/source_store.py
from hashlib import sha256
from pathlib import Path
import json
import mimetypes
import shutil
import uuid

from .models import SourceVersion


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SourceStore:
    def __init__(self, raw_root: Path):
        self.raw_root = raw_root

    def _find_hash(self, digest: str) -> SourceVersion | None:
        for manifest in self.raw_root.rglob("manifest.json"):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data["sha256"] == digest:
                return SourceVersion(**data)
        return None

    def archive(
        self, incoming: Path, space: str, source_id: str | None = None,
        previous_version_id: str | None = None,
    ) -> SourceVersion:
        digest = file_sha256(incoming)
        existing = self._find_hash(digest)
        if existing:
            return existing
        source_id = source_id or f"src_{uuid.uuid4().hex[:16]}"
        version_id = f"ver_{digest[:16]}"
        target_dir = self.raw_root / space / source_id / version_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / incoming.name
        shutil.copy2(incoming, target)
        if file_sha256(target) != digest:
            raise IOError("archived copy checksum mismatch")
        relative = Path("10_raw") / space / source_id / version_id / incoming.name
        record = SourceVersion(
            source_id=source_id, version_id=version_id, space=space,
            original_name=incoming.name,
            relative_path=relative.as_posix(), sha256=digest,
            media_type=mimetypes.guess_type(incoming.name)[0] or "application/octet-stream",
            status="archived", previous_version_id=previous_version_id,
        )
        (target_dir / "manifest.json").write_text(
            json.dumps(record.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record
```

- [ ] **Step 4: Verify originals and commit**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_source_store.py -v
git add src/local_kb/source_store.py tests/test_source_store.py
git commit -m "feat: archive immutable source versions"
```

Expected: PASS; test confirms v1 still exists after v2.

## Milestone 2 — Ingestion and compilation

### Task 4: Extractor registry and supported document formats

**Files:**
- Create: `src/local_kb/extractors/base.py`
- Create: `src/local_kb/extractors/text.py`
- Create: `src/local_kb/extractors/html.py`
- Create: `src/local_kb/extractors/pdf.py`
- Create: `src/local_kb/extractors/office.py`
- Create: `src/local_kb/extractors/unsupported.py`
- Create: `tests/test_extractors.py`

- [ ] **Step 1: Write table-driven extractor tests**

```python
# tests/test_extractors.py
from local_kb.extractors.base import registry


def test_text_extractor_preserves_line_locator(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("第一行\n第二行", encoding="utf-8")
    result = registry.extract(path)
    assert result.status == "extracted"
    assert result.fragments[1].locator == "lines:2-2"
    assert result.fragments[1].text == "第二行"


def test_unknown_media_is_pending_not_fabricated(tmp_path):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"not-a-real-video")
    result = registry.extract(path)
    assert result.status == "pending_extractor"
    assert result.fragments == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_extractors.py -v`  
Expected: FAIL because registry and result types are absent.

- [ ] **Step 3: Implement the registry contract**

```python
# src/local_kb/extractors/base.py
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Fragment:
    locator: str
    text: str


@dataclass(frozen=True)
class Extraction:
    status: str
    fragments: list[Fragment]
    warning: str | None = None


class Extractor(Protocol):
    suffixes: set[str]
    def extract(self, path: Path) -> Extraction: ...


class Registry:
    def __init__(self) -> None:
        self._items: list[Extractor] = []

    def register(self, extractor: Extractor) -> None:
        self._items.append(extractor)

    def extract(self, path: Path) -> Extraction:
        suffix = path.suffix.lower()
        for item in self._items:
            if suffix in item.suffixes:
                return item.extract(path)
        return Extraction("pending_extractor", [], f"no extractor for {suffix or 'no suffix'}")


registry = Registry()
```

- [ ] **Step 4: Implement text and HTML extraction**

```python
# src/local_kb/extractors/text.py
from pathlib import Path
from .base import Extraction, Fragment, registry


class TextExtractor:
    suffixes = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts"}

    def extract(self, path: Path) -> Extraction:
        text = path.read_text(encoding="utf-8", errors="replace")
        return Extraction(
            "extracted",
            [Fragment(f"lines:{n}-{n}", line) for n, line in enumerate(text.splitlines(), 1) if line.strip()],
        )


registry.register(TextExtractor())
```

```python
# src/local_kb/extractors/html.py
from pathlib import Path
from bs4 import BeautifulSoup
from .base import Extraction, Fragment, registry


def extract_html(path: Path) -> Extraction:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for node in soup(["script", "style", "nav"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    body = soup.get_text("\n", strip=True)
    return Extraction("extracted", [Fragment(f"title:{title}", body)])


class HtmlExtractor:
    suffixes = {".html", ".htm"}
    extract = staticmethod(extract_html)


registry.register(HtmlExtractor())
```

- [ ] **Step 5: Implement PDF and Office locators**

```python
# src/local_kb/extractors/pdf.py
from pathlib import Path
from pypdf import PdfReader
from .base import Extraction, Fragment, registry


def extract_pdf(path: Path) -> Extraction:
    reader = PdfReader(path)
    fragments = [
        Fragment(f"page:{index}", page.extract_text() or "")
        for index, page in enumerate(reader.pages, 1)
    ]
    fragments = [f for f in fragments if f.text.strip()]
    status = "extracted" if fragments else "pending_extractor"
    warning = None if fragments else "PDF has no extractable text; OCR required"
    return Extraction(status, fragments, warning)


class PdfExtractor:
    suffixes = {".pdf"}
    extract = staticmethod(extract_pdf)


registry.register(PdfExtractor())
```

```python
# src/local_kb/extractors/office.py
from pathlib import Path
from docx import Document
from openpyxl import load_workbook
from .base import Extraction, Fragment, registry


def extract_docx(path: Path) -> Extraction:
    doc = Document(path)
    return Extraction("extracted", [
        Fragment(f"paragraph:{i}", p.text)
        for i, p in enumerate(doc.paragraphs, 1) if p.text.strip()
    ])


def extract_xlsx(path: Path) -> Extraction:
    book = load_workbook(path, read_only=True, data_only=True)
    fragments: list[Fragment] = []
    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            values = ["" if cell.value is None else str(cell.value) for cell in row]
            if any(values):
                locator = f"sheet:{sheet.title};cells:{row[0].coordinate}-{row[-1].coordinate}"
                fragments.append(Fragment(locator, "\t".join(values)))
    return Extraction("extracted", fragments)


class DocxExtractor:
    suffixes = {".docx"}
    extract = staticmethod(extract_docx)


class XlsxExtractor:
    suffixes = {".xlsx", ".xlsm"}
    extract = staticmethod(extract_xlsx)


registry.register(DocxExtractor())
registry.register(XlsxExtractor())
```

Create `extractors/__init__.py` with these exact imports so registration always occurs:

```python
from . import html, office, pdf, text  # noqa: F401
from .base import registry

__all__ = ["registry"]
```

Do not register OCR or media suffixes; the base registry returns `pending_extractor`.

- [ ] **Step 6: Run extractor tests and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_extractors.py -v
git add src/local_kb/extractors tests/test_extractors.py
git commit -m "feat: extract local text and office evidence"
```

Expected: PASS.

### Task 5: Durable job queue, stable-file watcher, and ingestion orchestration

**Files:**
- Create: `src/local_kb/queue.py`
- Create: `src/local_kb/watcher.py`
- Create: `src/local_kb/ingest.py`
- Create: `tests/test_queue_ingest.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write failure, retry, and stability tests**

```python
# tests/test_queue_ingest.py
from local_kb.queue import DiskQueue
from local_kb.watcher import StableTracker


def test_job_moves_to_attention_after_three_failures(tmp_path):
    queue = DiskQueue(tmp_path, max_retries=3)
    job = queue.enqueue("C:/input/a.txt")
    queue.fail(job.job_id, "boom")
    queue.fail(job.job_id, "boom")
    final = queue.fail(job.job_id, "boom")
    assert final.state == "pending_attention"


def test_file_requires_two_unchanged_observations(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")
    tracker = StableTracker(required_seconds=0)
    assert tracker.observe(path) is False
    assert tracker.observe(path) is True
```

- [ ] **Step 2: Run and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_queue_ingest.py -v`  
Expected: FAIL because queue and tracker are missing.

- [ ] **Step 3: Implement atomic JSON jobs**

```python
# src/local_kb/queue.py
from dataclasses import replace
from pathlib import Path
import json
import shutil
import os
import uuid

from .models import Job


class DiskQueue:
    def __init__(self, root: Path, max_retries: int = 3):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def _write(self, job: Job) -> None:
        target = self._path(job.job_id)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)

    def enqueue(self, source_path: str) -> Job:
        job = Job(job_id=f"job_{uuid.uuid4().hex}", source_path=source_path)
        self._write(job)
        return job

    def get(self, job_id: str) -> Job:
        return Job(**json.loads(self._path(job_id).read_text(encoding="utf-8")))

    def fail(self, job_id: str, error: str) -> Job:
        old = self.get(job_id)
        attempts = old.attempts + 1
        state = "pending_attention" if attempts >= self.max_retries else "retrying"
        new = replace(old, attempts=attempts, state=state, error=error)
        self._write(new)
        return new
```

- [ ] **Step 4: Implement stable-file polling**

```python
# src/local_kb/watcher.py
from pathlib import Path
import time


class StableTracker:
    def __init__(self, required_seconds: float):
        self.required_seconds = required_seconds
        self._seen: dict[Path, tuple[int, int, float]] = {}

    def observe(self, path: Path) -> bool:
        stat = path.stat()
        now = time.monotonic()
        previous = self._seen.get(path)
        current = (stat.st_size, stat.st_mtime_ns, now)
        self._seen[path] = current
        if previous is None:
            return False
        unchanged = previous[:2] == current[:2]
        return unchanged and now - previous[2] >= self.required_seconds
```

- [ ] **Step 5: Implement `IngestService.process` as the state coordinator**

```python
# src/local_kb/ingest.py
from dataclasses import replace
from pathlib import Path
import json

from .catalog import Catalog
from .extractors.base import registry
from .queue import DiskQueue
from .source_store import SourceStore


class IngestService:
    def __init__(self, vault: Path, queue: DiskQueue, catalog: Catalog):
        self.vault = vault
        self.queue = queue
        self.catalog = catalog
        self.store = SourceStore(vault / "10_raw")

    def process(self, job_id: str, space: str = "unclassified"):
        job = self.queue.get(job_id)
        try:
            incoming = Path(job.source_path)
            previous = self.catalog.latest_source(space, incoming.name)
            source = self.store.archive(
                incoming,
                space,
                source_id=previous.source_id if previous else None,
                previous_version_id=previous.version_id if previous else None,
            )
            archived_path = self.vault / source.relative_path
            extraction = registry.extract(archived_path)
            indexed = replace(source, status=extraction.status)
            fragments = [(f.locator, f.text) for f in extraction.fragments]
            self.catalog.upsert_source(indexed, fragments)
            cache = self.vault / "40_index" / "cache" / indexed.version_id
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.with_suffix(".json").write_text(
                json.dumps(
                    {"source": indexed.__dict__, "fragments": fragments},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            processed = self.vault / "99_trash" / "processed-inbox" / job.job_id
            processed.mkdir(parents=True, exist_ok=True)
            shutil.move(str(incoming), processed / incoming.name)
            return indexed, extraction
        except Exception as exc:
            self.queue.fail(job_id, f"{type(exc).__name__}: {exc}")
            raise
```

- [ ] **Step 6: Add CLI commands and commit**

Add `watch` and `ingest-once` parsers to `cli.py`. `watch` loops over files in `00_inbox`, submits stable unseen files, and sleeps `poll_seconds`; `ingest-once <path> --space <space>` creates and processes one job. Both commands must print the resulting `version_id` and status.

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_queue_ingest.py -v
git add src/local_kb/queue.py src/local_kb/watcher.py src/local_kb/ingest.py src/local_kb/cli.py tests/test_queue_ingest.py
git commit -m "feat: ingest stable inbox files through durable queue"
```

Expected: PASS.

### Task 6: Wiki schema, validation, atomic publish, and Git version

**Files:**
- Create: `src/local_kb/wiki.py`
- Create: `src/local_kb/transaction.py`
- Create: `tests/test_wiki_transaction.py`

- [ ] **Step 1: Write failing validation and rollback tests**

```python
# tests/test_wiki_transaction.py
import pytest
from local_kb.transaction import ChangeTransaction
from local_kb.wiki import WikiPage, validate_page


def test_page_requires_raw_source():
    page = WikiPage("c1", "測試", "concept", "work", "high", [], "內容", "", "")
    with pytest.raises(ValueError, match="source"):
        validate_page(page)


def test_invalid_batch_never_reaches_live_wiki(tmp_path):
    tx = ChangeTransaction(tmp_path)
    tx.stage("20_wiki/work/concepts/a.md", "invalid")
    with pytest.raises(ValueError):
        tx.publish(lambda _: (_ for _ in ()).throw(ValueError("bad")))
    assert not (tmp_path / "20_wiki/work/concepts/a.md").exists()
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_wiki_transaction.py -v`  
Expected: FAIL because Wiki and transaction types are absent.

- [ ] **Step 3: Implement deterministic Wiki rendering**

```python
# src/local_kb/wiki.py
from dataclasses import dataclass


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    title: str
    page_type: str
    space: str
    confidence: str
    source_ids: list[str]
    current_state: str
    conflicts: str
    timeline_entry: str


def validate_page(page: WikiPage) -> None:
    if not page.source_ids:
        raise ValueError("source_ids must contain at least one raw source")
    if page.confidence not in {"high", "medium", "low"}:
        raise ValueError("invalid confidence")
    if page.space not in {"personal", "work", "shared", "unclassified"} and not page.space.startswith("project:"):
        raise ValueError("invalid space")


def render_page(page: WikiPage) -> str:
    validate_page(page)
    sources = "\n".join(f"  - {s}" for s in page.source_ids)
    return f"""---
id: {page.page_id}
title: {page.title}
type: {page.page_type}
space: {page.space}
confidence: {page.confidence}
source_ids:
{sources}
---

## Current State

{page.current_state}

## Evidence

{sources}

## Conflicts and Gaps

{page.conflicts or "無"}

## Timeline

{page.timeline_entry}
"""
```

- [ ] **Step 4: Implement staging and atomic replace**

```python
# src/local_kb/transaction.py
from pathlib import Path
import os
import shutil
import subprocess
import uuid


class ChangeTransaction:
    def __init__(self, vault: Path):
        self.vault = vault
        self.stage_root = vault / ".kb" / "staging" / uuid.uuid4().hex
        self.stage_root.mkdir(parents=True)

    def stage(self, relative_path: str, content: str) -> None:
        target = self.stage_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def publish(self, validator) -> None:
        validator(self.stage_root)
        for staged in self.stage_root.rglob("*"):
            if not staged.is_file():
                continue
            relative = staged.relative_to(self.stage_root)
            live = self.vault / relative
            live.parent.mkdir(parents=True, exist_ok=True)
            temp = live.with_suffix(live.suffix + ".new")
            shutil.copy2(staged, temp)
            os.replace(temp, live)
        shutil.rmtree(self.stage_root)

    def commit_git(self, message: str) -> None:
        subprocess.run(["git", "add", "20_wiki", "30_answers", "40_index/index.md", "90_logs"],
                       cwd=self.vault, check=True)
        subprocess.run(["git", "-c", "user.name=Local Knowledge Compiler",
                        "-c", "user.email=kb@local", "-c", "commit.gpgsign=false",
                        "commit", "-m", message],
                       cwd=self.vault, check=True)
```

- [ ] **Step 5: Verify rollback and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_wiki_transaction.py -v
git add src/local_kb/wiki.py src/local_kb/transaction.py tests/test_wiki_transaction.py
git commit -m "feat: validate and atomically publish wiki changes"
```

Expected: PASS.

### Task 7: Model-neutral compiler with Claude and manual adapters

**Files:**
- Create: `src/local_kb/compiler.py`
- Create: `tests/test_compiler.py`
- Modify: `src/local_kb/ingest.py`

- [ ] **Step 1: Write mocked adapter tests**

```python
# tests/test_compiler.py
from local_kb.compiler import ClaudeCompiler, ManualCompiler


def test_claude_compiler_disables_tools_and_requires_json(monkeypatch, tmp_path):
    captured = {}
    def fake_run(command, **kwargs):
        captured["command"] = command
        class Result:
            stdout = '{"result":{"changes":[]}}'
        return Result()
    monkeypatch.setattr("local_kb.compiler.subprocess.run", fake_run)
    result = ClaudeCompiler().compile("evidence")
    assert "--tools" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert result == {"changes": []}


def test_manual_compiler_exports_job_without_claiming_success(tmp_path):
    path = ManualCompiler(tmp_path).compile("evidence")
    assert path.exists()
    assert path.suffix == ".json"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_compiler.py -v`  
Expected: FAIL because compiler adapters are absent.

- [ ] **Step 3: Implement strict structured Claude invocation**

```python
# src/local_kb/compiler.py
from pathlib import Path
import json
import subprocess
import uuid


OUTPUT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string"},
                    "space": {"type": "string"},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "current_state": {"type": "string"},
                    "conflicts": {"type": "string"},
                    "timeline_entry": {"type": "string"},
                },
                "required": ["path", "title", "type", "space", "confidence",
                             "source_ids", "current_state", "conflicts", "timeline_entry"],
            },
        }
    },
    "required": ["changes"],
})


class ClaudeCompiler:
    def compile(self, evidence: str) -> dict:
        prompt = (
            "只根據下列本地證據提出 Wiki 變更。不得上網，不得加入模型常識。"
            "每項事實必須保留 source_id。\n\n" + evidence
        )
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--permission-mode", "dontAsk",
             "--tools", "", "--no-session-persistence", "--json-schema", OUTPUT_SCHEMA, prompt],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        envelope = json.loads(result.stdout)
        payload = envelope["result"]
        return json.loads(payload) if isinstance(payload, str) else payload


class ManualCompiler:
    def __init__(self, outbox: Path):
        self.outbox = outbox

    def compile(self, evidence: str) -> Path:
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"manual_{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"status": "needs_agent", "evidence": evidence},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return path
```

Codex Desktop executable currently cannot be launched as a background CLI on this machine (`Access is denied`). Therefore the first implementation uses Claude CLI for immediate background compilation and the manual adapter as the fail-safe. Codex remains a fully supported interactive client through `AGENTS.md`, `kb prepare`, and `kb finalize`.

- [ ] **Step 4: Convert compiler changes to validated Wiki pages**

Add these imports and methods to `ingest.py`:

```python
from hashlib import sha256
from pathlib import PurePosixPath

from .transaction import ChangeTransaction
from .wiki import WikiPage, render_page


def _safe_wiki_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("20_wiki",):
        raise ValueError(f"compiler path outside wiki: {value}")
    if path.suffix != ".md":
        raise ValueError(f"compiler path is not markdown: {value}")
    return path.as_posix()


def compile_extraction(self, source, extraction) -> list[str]:
    evidence = "\n".join(
        f"source_id={source.source_id} locator={fragment.locator}\n{fragment.text}"
        for fragment in extraction.fragments
    )
    result = self.compiler.compile(evidence)
    if isinstance(result, Path):
        return []
    transaction = ChangeTransaction(self.vault)
    staged: list[str] = []
    for change in result["changes"]:
        relative = _safe_wiki_path(change["path"])
        page = WikiPage(
            page_id="page_" + sha256(relative.encode("utf-8")).hexdigest()[:16],
            title=change["title"],
            page_type=change["type"],
            space=change["space"],
            confidence=change["confidence"],
            source_ids=list(change["source_ids"]),
            current_state=change["current_state"],
            conflicts=change["conflicts"],
            timeline_entry=change["timeline_entry"],
        )
        transaction.stage(relative, render_page(page))
        staged.append(relative)
    transaction.publish(lambda _: None)
    transaction.commit_git(f"kb: compile {source.version_id}")
    return staged
```

Modify `IngestService.__init__` exactly as follows:

```python
from .compiler import ManualCompiler


def __init__(self, vault: Path, queue: DiskQueue, catalog: Catalog, compiler=None):
    self.vault = vault
    self.queue = queue
    self.catalog = catalog
    self.store = SourceStore(vault / "10_raw")
    self.compiler = compiler or ManualCompiler(vault / ".kb" / "manual")
```

After extraction, call `compile_extraction` only when the status is `extracted`; `pending_extractor` is indexed by metadata but never sent to the model. The manual default keeps tests and Codex-only use functional without silently claiming that background compilation occurred.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_compiler.py tests/test_wiki_transaction.py -v
git add src/local_kb/compiler.py src/local_kb/ingest.py tests/test_compiler.py
git commit -m "feat: compile evidence through replaceable agent adapter"
```

Expected: PASS.

## Milestone 3 — Retrieval and knowledge feedback

### Task 8: Multi-route search and `kb prepare`

**Files:**
- Create: `src/local_kb/search.py`
- Create: `src/local_kb/query.py`
- Create: `tests/test_prepare.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write tests for space isolation, evidence priority, and no-result honesty**

```python
# tests/test_prepare.py
from local_kb.query import QueryService


def test_prepare_excludes_personal_from_work_question(seeded_catalog):
    packet = QueryService(seeded_catalog).prepare("工作專案目前決策", {"work"})
    assert all(hit["space"] == "work" for hit in packet["evidence"])


def test_prepare_reports_no_evidence(seeded_catalog):
    packet = QueryService(seeded_catalog).prepare("完全不存在的主題", {"work"})
    assert packet["status"] == "insufficient_evidence"
    assert packet["evidence"] == []
```

- [ ] **Step 2: Run and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_prepare.py -v`  
Expected: FAIL because `QueryService` is absent.

- [ ] **Step 3: Implement query normalization and ranking**

```python
# src/local_kb/search.py
import re
from .catalog import Catalog


def normalize_query(question: str) -> str:
    tokens = re.findall(r"[\w\u3400-\u9fff]+", question.lower())
    return " OR ".join(dict.fromkeys(tokens))


def ranked_search(catalog: Catalog, question: str, spaces: set[str]):
    query = normalize_query(question)
    if not query:
        return []
    hits = catalog.search(query, spaces, limit=40)
    return sorted(hits, key=lambda h: h.score, reverse=True)[:12]
```

- [ ] **Step 4: Build a stable JSON evidence packet**

```python
# src/local_kb/query.py
from datetime import datetime, timezone
from .search import ranked_search


class QueryService:
    def __init__(self, catalog):
        self.catalog = catalog

    def prepare(self, question: str, spaces: set[str]) -> dict:
        hits = ranked_search(self.catalog, question, spaces)
        evidence = [
            {
                "source_id": h.source_id,
                "version_id": h.version_id,
                "path": h.relative_path,
                "locator": h.locator,
                "text": h.text,
                "score": round(h.score, 6),
                "space": h.space,
            }
            for h in hits
        ]
        return {
            "question": question,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "spaces": sorted(spaces),
            "status": "ready" if evidence else "insufficient_evidence",
            "instructions": [
                "只使用 evidence 回答",
                "使用繁體中文",
                "列出來源、衝突、信心與未知事項",
                "沒有證據時明確說目前無法確定",
            ],
            "evidence": evidence,
        }
```

- [ ] **Step 5: Add `prepare` CLI output and commit**

`kb prepare "<question>" --space work --output .kb/last-packet.json` must write UTF-8 JSON and print the absolute packet path. Before searching, it must count related queue jobs; if jobs remain, add `pending_jobs` to the packet instead of hiding them.

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_prepare.py -v
git add src/local_kb/search.py src/local_kb/query.py src/local_kb/cli.py tests/test_prepare.py
git commit -m "feat: prepare grounded local evidence packets"
```

Expected: PASS.

### Task 9: `kb finalize`, answer provenance, and derived-knowledge safeguards

**Files:**
- Create: `src/local_kb/finalize.py`
- Create: `tests/test_finalize.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write failing answer-save tests**

```python
# tests/test_finalize.py
import json
import pytest
from local_kb.finalize import finalize_answer


def test_finalize_rejects_unknown_source(tmp_path):
    packet = {"question": "Q", "evidence": [{"source_id": "src_ok"}]}
    answer = {"conclusion": "A", "citations": ["src_missing"], "confidence": "high"}
    with pytest.raises(ValueError, match="unknown citation"):
        finalize_answer(tmp_path, packet, answer)


def test_finalize_saves_answer_with_provenance(tmp_path):
    packet = {"question": "Q", "evidence": [{"source_id": "src_ok"}]}
    answer = {"conclusion": "A", "citations": ["src_ok"], "confidence": "medium"}
    path = finalize_answer(tmp_path, packet, answer)
    text = path.read_text(encoding="utf-8")
    assert "src_ok" in text
    assert "衍生知識" in text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_finalize.py -v`  
Expected: FAIL because `finalize_answer` is absent.

- [ ] **Step 3: Implement citation validation and answer rendering**

```python
# src/local_kb/finalize.py
from datetime import datetime, timezone
from pathlib import Path
import uuid


def finalize_answer(vault: Path, packet: dict, answer: dict) -> Path:
    allowed = {item["source_id"] for item in packet["evidence"]}
    cited = set(answer.get("citations", []))
    unknown = cited - allowed
    if unknown:
        raise ValueError(f"unknown citation: {sorted(unknown)}")
    now = datetime.now(timezone.utc)
    target = vault / "30_answers" / f"{now:%Y}" / f"{now:%Y-%m-%d}-{uuid.uuid4().hex[:8]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    citations = "\n".join(f"- {source_id}" for source_id in sorted(cited))
    target.write_text(
        f"""---
type: derived-answer
label: 衍生知識
confidence: {answer.get("confidence", "low")}
created_at: {now.isoformat()}
---

# {packet["question"]}

## 直接結論

{answer.get("conclusion", "目前無法確定")}

## 來源

{citations or "- 無可用來源"}

## 衝突與未知

{answer.get("conflicts", "無")}
""",
        encoding="utf-8",
    )
    return target
```

- [ ] **Step 4: Add finalize CLI and derived update job**

`kb finalize --packet <packet.json> --answer <answer.json>` must:

1. validate every citation against the packet;
2. save the answer;
3. enqueue a `derived_update` job carrying the cited raw source IDs;
4. never index the answer as a raw source;
5. print the saved Markdown path and queued job ID.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_finalize.py -v
git add src/local_kb/finalize.py src/local_kb/cli.py tests/test_finalize.py
git commit -m "feat: finalize cited answers without self-reinforcement"
```

Expected: PASS.

## Milestone 4 — Reliability and agent integration

### Task 10: Single-writer lock, crash recovery, lint, and index rebuild

**Files:**
- Modify: `src/local_kb/queue.py`
- Create: `src/local_kb/health.py`
- Create: `tests/test_health_concurrency.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write lock and rebuild tests**

```python
# tests/test_health_concurrency.py
import pytest
from local_kb.queue import WriterLock
from local_kb.health import rebuild_catalog


def test_second_writer_cannot_enter(tmp_path):
    with WriterLock(tmp_path / "write.lock"):
        with pytest.raises(TimeoutError):
            with WriterLock(tmp_path / "write.lock", timeout=0):
                pass


def test_rebuild_restores_search_from_cache(vault_with_cache):
    db = vault_with_cache / "40_index" / "catalog.sqlite"
    db.unlink()
    count = rebuild_catalog(vault_with_cache)
    assert count == 1
    assert db.exists()
```

- [ ] **Step 2: Run and confirm failure**

Run: `.\.venv\Scripts\pytest.exe tests/test_health_concurrency.py -v`  
Expected: FAIL because lock and rebuild functions are absent.

- [ ] **Step 3: Implement an exclusive Windows-safe lock**

```python
# append to src/local_kb/queue.py
from time import monotonic, sleep


class WriterLock:
    def __init__(self, path: Path, timeout: float = 30):
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        deadline = monotonic() + self.timeout
        while True:
            try:
                self.handle = self.path.open("x", encoding="utf-8")
                self.handle.write(str(os.getpid()))
                self.handle.flush()
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="utf-8"))
                    os.kill(pid, 0)
                except (ValueError, ProcessLookupError):
                    recovery = self.path.parents[1] / "90_logs" / "recovery"
                    recovery.mkdir(parents=True, exist_ok=True)
                    stale = recovery / f"stale-{self.path.name}-{int(monotonic())}"
                    os.replace(self.path, stale)
                    continue
                except PermissionError:
                    pass
                if monotonic() >= deadline:
                    raise TimeoutError("writer lock unavailable")
                sleep(0.1)

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            self.handle.close()
        self.path.unlink(missing_ok=True)
```

- [ ] **Step 4: Implement health report and rebuild**

```python
# src/local_kb/health.py
from pathlib import Path
import json

from .catalog import Catalog
from .models import SourceVersion


def rebuild_catalog(vault: Path) -> int:
    db = vault / "40_index" / "catalog.sqlite"
    db.unlink(missing_ok=True)
    catalog = Catalog(db)
    catalog.initialize()
    count = 0
    cache = vault / "40_index" / "cache"
    for meta in cache.rglob("*.json"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        catalog.upsert_source(
            SourceVersion(**data["source"]),
            [(locator, text) for locator, text in data["fragments"]],
        )
        count += 1
    return count


def lint(vault: Path) -> dict:
    wiki = list((vault / "20_wiki").rglob("*.md"))
    missing_source = [str(p.relative_to(vault)) for p in wiki if "source_ids:" not in p.read_text(encoding="utf-8")]
    pending = list((vault / ".kb" / "queue").glob("*.json"))
    return {
        "wiki_pages": len(wiki),
        "missing_source_pages": missing_source,
        "pending_jobs": len(pending),
        "healthy": not missing_source,
    }
```

- [ ] **Step 5: Add CLI commands, run tests, and commit**

```powershell
.\.venv\Scripts\pytest.exe tests/test_health_concurrency.py -v
git add src/local_kb/queue.py src/local_kb/health.py src/local_kb/cli.py tests/test_health_concurrency.py
git commit -m "feat: recover writers and rebuild knowledge index"
```

Expected: PASS. `kb lint` prints JSON; `kb rebuild` prints indexed source count.

### Task 11: Shared protocol, Codex/Claude entry files, Windows launcher, and end-to-end test

**Files:**
- Create: `src/local_kb/templates/KNOWLEDGE_PROTOCOL.md`
- Create: `src/local_kb/templates/AGENTS.md`
- Create: `src/local_kb/templates/CLAUDE.md`
- Create: `scripts/start-kb.ps1`
- Create: `README.md`
- Create: `tests/test_e2e.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write the end-to-end acceptance test**

```python
# tests/test_e2e.py
import json
from local_kb.cli import build_vault
from local_kb.catalog import Catalog
from local_kb.ingest import IngestService
from local_kb.queue import DiskQueue
from local_kb.query import QueryService


def test_ingest_prepare_finalize_without_network(tmp_path):
    build_vault(tmp_path)
    source = tmp_path / "00_inbox" / "decision.md"
    source.write_text("# 決策\n採用 B 架構。", encoding="utf-8")
    queue = DiskQueue(tmp_path / ".kb" / "queue")
    catalog = Catalog(tmp_path / "40_index" / "catalog.sqlite")
    catalog.initialize()
    job = queue.enqueue(str(source))
    IngestService(tmp_path, queue, catalog).process(job.job_id, "work")
    packet = QueryService(catalog).prepare("採用哪個架構", {"work"})
    assert packet["status"] == "ready"
    assert any("B 架構" in item["text"] for item in packet["evidence"])
    assert all("http" not in item["path"] for item in packet["evidence"])
```

- [ ] **Step 2: Write the canonical protocol**

```markdown
<!-- src/local_kb/templates/KNOWLEDGE_PROTOCOL.md -->
# Knowledge Protocol

1. 回答知識庫問題前，先執行 `kb prepare "<問題>" --space <範圍>`。
2. 只使用證據包中的內容；不得搜尋網路或補入模型記憶。
3. 固定輸出：直接結論、證據整理、來源、衝突與時效、信心、未知事項。
4. 若證據包狀態為 `insufficient_evidence`，明確回答「目前本地資料無法確定」。
5. 回答完成後，把結構化答案存成 JSON，執行 `kb finalize`。
6. 不得直接修改 `10_raw`；Wiki 修改一律進入工作佇列。
7. personal、work、project、shared 預設隔離；跨區只在問題明確要求時使用。
```

- [ ] **Step 3: Write thin agent entry files**

```markdown
<!-- src/local_kb/templates/AGENTS.md -->
# Local Knowledge Base

處理知識問題前，必須完整讀取 `80_system/KNOWLEDGE_PROTOCOL.md` 並遵守。
本知識庫只允許本地證據，不得自動搜尋網路。
```

```markdown
<!-- src/local_kb/templates/CLAUDE.md -->
# Local Knowledge Base

Before answering knowledge questions, read and follow
`80_system/KNOWLEDGE_PROTOCOL.md` completely.
Use local evidence only. Do not search the web.
Return the answer in Traditional Chinese.
```

Update `build_vault` to copy these templates into their approved target locations without overwriting user-edited files on repeated `kb init`.

- [ ] **Step 4: Add a visible Windows launcher**

```powershell
# scripts/start-kb.ps1
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "尚未建立 .venv，請先依 README 執行安裝。"
}
& $Python -m local_kb.cli watch
```

The launcher must not hide errors. For automatic login startup, README instructs the user to place a shortcut to this PowerShell script in `shell:startup`; the implementation must not modify Windows startup automatically.

- [ ] **Step 5: Write beginner README with exact daily workflow**

README must contain these exact flows:

```text
第一次：建立 .venv → 安裝套件 → kb init <知識庫路徑> → 啟動 watcher
新增資料：把本地檔案拖入 00_inbox
提問：在 Codex 或 Claude 直接提問；agent 會先執行 kb prepare
檢查：kb lint
復原：git log → git revert <commit>
```

It must state clearly:

- Codex／Claude are cloud models; local storage does not mean submitted evidence stays offline.
- Bare URLs are bookmarks only and are never fetched.
- Images, scanned PDFs, audio, and video initially show `pending_extractor`.
- The background compiler currently uses Claude CLI because the installed Codex Desktop executable cannot be invoked as a CLI on this machine.

- [ ] **Step 6: Run the complete verification suite**

```powershell
.\.venv\Scripts\pytest.exe -v
.\.venv\Scripts\kb.exe init .\acceptance-vault
.\.venv\Scripts\kb.exe lint --vault .\acceptance-vault
git status --short
```

Expected:

- all tests PASS;
- vault tree and three protocol files exist;
- lint JSON contains `"healthy": true`;
- Git shows only the intended implementation files.

- [ ] **Step 7: Commit the completed first version**

```powershell
git add README.md scripts src tests pyproject.toml
git commit -m "feat: deliver local knowledge compiler v1"
```

## Final acceptance run

- [ ] Copy one Markdown file, one PDF, one DOCX, one XLSX, and one MP4 into a fresh `00_inbox`.
- [ ] Confirm Markdown/PDF/DOCX/XLSX become `extracted` when they contain readable text.
- [ ] Confirm MP4 becomes `pending_extractor` and remains safely archived.
- [ ] Copy the same Markdown twice and confirm only one content version is indexed.
- [ ] Modify that Markdown and confirm the previous version remains in `10_raw`.
- [ ] Run two concurrent `kb finalize` processes and confirm the second waits or exits with a clear lock error without corrupting files.
- [ ] Delete `catalog.sqlite`, run `kb rebuild`, and confirm the same query results return.
- [ ] Ask a question with no local evidence and confirm the answer says it cannot be determined.
- [ ] Inspect one saved answer and confirm each citation appears in its evidence packet.
- [ ] Use `git revert` on a test Wiki commit and confirm raw sources are unchanged.

## Plan evidence

Dependency versions were checked against their official PyPI project pages on 2026-07-25. `pypdf` explicitly supports Python 3.13 and text extraction; `python-docx`, `openpyxl`, and Beautiful Soup cover the approved local document formats. `openpyxl` warns that XML protection is not enabled by default, so `defusedxml` is pinned as a required dependency.

## Self-review coverage

| Approved requirement | Implemented by |
|---|---|
| Pure local files, no Obsidian | Tasks 1, 3, 11 |
| Immutable raw sources and version chains | Task 3 |
| Immediate inbox processing | Task 5 |
| PDF, Word, Excel, text, local HTML | Task 4 |
| Honest pending state for OCR and media | Tasks 4, 11 |
| AI-maintained Wiki with citations | Tasks 6, 7 |
| Codex and Claude share one protocol | Tasks 7, 11 |
| Local-only evidence and no web search | Tasks 7, 8, 11 |
| Precise packet with space isolation and locators | Tasks 2, 4, 8 |
| Answer provenance and safe knowledge feedback | Task 9 |
| Concurrent readers and single writer | Tasks 6, 10 |
| Crash recovery, lint, and index rebuild | Task 10 |
| Git history and recoverable trash | Tasks 3, 5, 6 |
| Beginner setup and acceptance workflow | Task 11 and Final acceptance run |

Self-review found no uncovered approved requirement. OCR and media transcription are intentionally represented by the approved `pending_extractor` behavior rather than an unimplemented promise.
