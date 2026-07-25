"""A rebuildable SQLite catalog backed by FTS5."""

from collections.abc import Collection
from pathlib import Path
import sqlite3

from .models import SearchHit, SourceVersion, Space


class Catalog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
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
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_fragments (
                    version_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (version_id, locator)
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
                    version_id UNINDEXED,
                    source_id UNINDEXED,
                    relative_path UNINDEXED,
                    locator UNINDEXED,
                    body,
                    tokenize='unicode61'
                )
                """
            )

    def upsert_source(
        self, source: SourceVersion, fragments: list[tuple[str, str]]
    ) -> None:
        nonblank_fragments = [
            (locator, text) for locator, text in fragments if text.strip()
        ]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO sources (
                    version_id, source_id, space, original_name, relative_path,
                    sha256, media_type, status, previous_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.version_id,
                    source.source_id,
                    source.space,
                    source.original_name,
                    source.relative_path,
                    source.sha256,
                    source.media_type,
                    source.status,
                    source.previous_version_id,
                ),
            )
            connection.execute(
                "DELETE FROM source_fts WHERE version_id = ?", (source.version_id,)
            )
            connection.execute(
                "DELETE FROM source_fragments WHERE version_id = ?",
                (source.version_id,),
            )
            connection.executemany(
                """
                INSERT INTO source_fragments (version_id, locator, text)
                VALUES (?, ?, ?)
                """,
                [
                    (source.version_id, locator, text)
                    for locator, text in nonblank_fragments
                ],
            )
            connection.executemany(
                """
                INSERT INTO source_fts (
                    version_id, source_id, relative_path, locator, body
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        source.version_id,
                        source.source_id,
                        source.relative_path,
                        locator,
                        self._searchable_text(text),
                    )
                    for locator, text in nonblank_fragments
                ],
            )

    def latest_source(
        self, space: Space, original_name: str
    ) -> SourceVersion | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT version_id, source_id, space, original_name, relative_path,
                       sha256, media_type, status, previous_version_id
                FROM sources
                WHERE space = ? AND original_name = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (space, original_name),
            ).fetchone()
        return self._source_from_row(row) if row is not None else None

    def search(
        self, query: str, spaces: Collection[Space], limit: int = 20
    ) -> list[SearchHit]:
        if not spaces:
            return []

        placeholders = ", ".join("?" for _ in spaces)
        sql = f"""
            SELECT source_fts.version_id, source_fts.source_id, sources.space,
                   source_fts.relative_path, source_fts.locator, source_fragments.text,
                   -bm25(source_fts) AS score
            FROM source_fts
            JOIN sources ON sources.version_id = source_fts.version_id
            JOIN source_fragments
              ON source_fragments.version_id = source_fts.version_id
             AND source_fragments.locator = source_fts.locator
            WHERE source_fts MATCH ?
              AND sources.space IN ({placeholders})
            ORDER BY bm25(source_fts)
            LIMIT ?
        """
        with self.connect() as connection:
            rows = connection.execute(sql, (query, *spaces, limit)).fetchall()
        return [
            SearchHit(
                version_id=row["version_id"],
                source_id=row["source_id"],
                space=row["space"],
                relative_path=row["relative_path"],
                locator=row["locator"],
                text=row["text"],
                score=row["score"],
            )
            for row in rows
        ]

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceVersion:
        return SourceVersion(
            version_id=row["version_id"],
            source_id=row["source_id"],
            space=row["space"],
            original_name=row["original_name"],
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            media_type=row["media_type"],
            status=row["status"],
            previous_version_id=row["previous_version_id"],
        )

    @staticmethod
    def _searchable_text(text: str) -> str:
        """Add CJK substrings so unicode61 can find terms inside continuous text."""
        searchable = [text]
        start = 0
        while start < len(text):
            if not "\u3400" <= text[start] <= "\u9fff":
                start += 1
                continue
            end = start + 1
            while end < len(text) and "\u3400" <= text[end] <= "\u9fff":
                end += 1
            run = text[start:end]
            searchable.extend(
                run[index : index + length]
                for length in range(1, len(run) + 1)
                for index in range(len(run) - length + 1)
            )
            start = end
        return " ".join(searchable)
