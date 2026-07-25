"""A rebuildable SQLite catalog backed by FTS5."""

from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator, cast

from .models import SearchHit, SourceStatus, SourceVersion, Space


class Catalog:
    MAX_SEARCH_LIMIT = 100
    SCHEMA_VERSION = 2

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"catalog schema {current_version} is newer than supported "
                    f"schema {self.SCHEMA_VERSION}"
                )
            sources_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
            ).fetchone()
            if sources_exist is not None and current_version < self.SCHEMA_VERSION:
                self._drop_schema(connection)
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
                    status TEXT NOT NULL CHECK (status IN (
                        'archived', 'extracted', 'pending_extractor',
                        'compiled', 'validated', 'published'
                    )),
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
                    PRIMARY KEY (version_id, locator),
                    FOREIGN KEY (version_id) REFERENCES sources(version_id)
                        ON DELETE CASCADE
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
                    space UNINDEXED,
                    body,
                    tokenize='unicode61'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_fts_map (
                    fts_rowid INTEGER PRIMARY KEY,
                    version_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    FOREIGN KEY (version_id) REFERENCES sources(version_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_source_fts_map_version_id
                ON source_fts_map(version_id)
                """
            )
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @staticmethod
    def _drop_schema(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS source_fts")
        connection.execute("DROP TABLE IF EXISTS source_fts_map")
        connection.execute("DROP TABLE IF EXISTS source_fragments")
        connection.execute("DROP TABLE IF EXISTS sources")

    def upsert_source(
        self, source: SourceVersion, fragments: list[tuple[str, str]]
    ) -> None:
        nonblank_fragments = [
            (locator, text) for locator, text in fragments if text.strip()
        ]
        with self.connection() as connection:
            collision = connection.execute(
                "SELECT version_id FROM sources WHERE sha256 = ?",
                (source.sha256,),
            ).fetchone()
            if collision is not None and collision["version_id"] != source.version_id:
                raise ValueError(
                    f"sha256 already belongs to version {collision['version_id']}"
                )
            connection.execute(
                """
                INSERT INTO sources (
                    version_id, source_id, space, original_name, relative_path,
                    sha256, media_type, status, previous_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    space = excluded.space,
                    original_name = excluded.original_name,
                    relative_path = excluded.relative_path,
                    sha256 = excluded.sha256,
                    media_type = excluded.media_type,
                    status = excluded.status,
                    previous_version_id = excluded.previous_version_id
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
                """
                DELETE FROM source_fts
                WHERE rowid IN (
                    SELECT fts_rowid FROM source_fts_map WHERE version_id = ?
                )
                """,
                (source.version_id,),
            )
            connection.execute(
                "DELETE FROM source_fts_map WHERE version_id = ?",
                (source.version_id,),
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
            for locator, text in nonblank_fragments:
                cursor = connection.execute(
                    """
                    INSERT INTO source_fts (
                        version_id, source_id, relative_path, locator, space, body
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.version_id,
                        source.source_id,
                        source.relative_path,
                        locator,
                        source.space,
                        self._searchable_text(text),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_fts_map (fts_rowid, version_id, locator)
                    VALUES (?, ?, ?)
                    """,
                    (cursor.lastrowid, source.version_id, locator),
                )

    def latest_source(
        self, space: Space, original_name: str
    ) -> SourceVersion | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT candidate.version_id, candidate.source_id, candidate.space,
                       candidate.original_name, candidate.relative_path,
                       candidate.sha256, candidate.media_type, candidate.status,
                       candidate.previous_version_id
                FROM sources AS candidate
                WHERE candidate.space = ? AND candidate.original_name = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM sources AS successor
                      WHERE successor.previous_version_id = candidate.version_id
                  )
                ORDER BY candidate.version_id DESC
                LIMIT 1
                """,
                (space, original_name),
            ).fetchone()
        return self._source_from_row(row) if row is not None else None

    def search(
        self, query: str, spaces: Collection[Space], limit: int = 20
    ) -> list[SearchHit]:
        if isinstance(spaces, str):
            raise TypeError("spaces must be a collection of space names, not a string")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.MAX_SEARCH_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {self.MAX_SEARCH_LIMIT}")
        if not spaces:
            return []

        match_query = self._plain_match_query(query)
        if not match_query:
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
        with self.connection() as connection:
            rows = connection.execute(sql, (match_query, *spaces, limit)).fetchall()
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
            status=cast(SourceStatus, row["status"]),
            previous_version_id=row["previous_version_id"],
        )

    @staticmethod
    def _searchable_text(text: str) -> str:
        """Add bounded CJK n-grams for unicode61 tokenization."""
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
            searchable.extend(Catalog._cjk_ngrams(run))
            start = end
        return " ".join(searchable)

    @staticmethod
    def _cjk_ngrams(text: str) -> list[str]:
        return [
            text[index : index + size]
            for size in range(1, min(3, len(text)) + 1)
            for index in range(len(text) - size + 1)
        ]

    @staticmethod
    def _plain_match_query(query: str) -> str:
        terms: list[str] = []
        start = 0
        while start < len(query):
            character = query[start]
            if "\u3400" <= character <= "\u9fff":
                end = start + 1
                while end < len(query) and "\u3400" <= query[end] <= "\u9fff":
                    end += 1
                terms.extend(Catalog._cjk_ngrams(query[start:end]))
                start = end
            elif character.isalnum() or character == "_":
                end = start + 1
                while end < len(query) and (
                    query[end].isalnum() or query[end] == "_"
                ) and not "\u3400" <= query[end] <= "\u9fff":
                    end += 1
                terms.append(query[start:end])
                start = end
            else:
                start += 1
        return " AND ".join(f'"{term}"' for term in terms)
