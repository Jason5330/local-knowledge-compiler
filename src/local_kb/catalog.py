"""A rebuildable SQLite catalog backed by FTS5."""

from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator, cast

from .models import SearchHit, SourceStatus, SourceVersion, Space
from .safety import guarded_catalog_path, secure_directory, verify_catalog_paths


class _GuardedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        self.connection._verify_catalog_guard()
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self.connection._verify_catalog_guard()
        return super().executemany(sql, parameters)

    def executescript(self, sql_script, /):
        self.connection._verify_catalog_guard()
        return super().executescript(sql_script)


class _GuardedConnection(sqlite3.Connection):
    _catalog_guard = None
    _catalog_path: Path | None = None

    def _verify_catalog_guard(self) -> None:
        if self._catalog_guard is None or self._catalog_path is None:
            raise sqlite3.ProgrammingError("catalog connection is closed")
        verify_catalog_paths(self._catalog_path)

    def execute(self, sql, parameters=(), /):
        self._verify_catalog_guard()
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self._verify_catalog_guard()
        return super().executemany(sql, parameters)

    def executescript(self, sql_script, /):
        self._verify_catalog_guard()
        return super().executescript(sql_script)

    def cursor(self, factory=_GuardedCursor):
        self._verify_catalog_guard()
        return super().cursor(factory)

    def commit(self) -> None:
        self._verify_catalog_guard()
        super().commit()

    def rollback(self) -> None:
        self._verify_catalog_guard()
        super().rollback()

    def close(self) -> None:
        guard = self._catalog_guard
        if guard is None:
            return super().close()
        self._catalog_guard = None
        try:
            super().close()
        finally:
            guard.__exit__(None, None, None)

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass


class Catalog:
    MAX_SEARCH_LIMIT = 100
    MAX_QUERY_CHARACTERS = 256
    MAX_QUERY_TERMS = 64
    SCHEMA_VERSION = 4

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        guard = guarded_catalog_path(self.path, allow_missing_main=True)
        path = guard.__enter__()
        connection: _GuardedConnection | None = None
        try:
            connection = sqlite3.connect(path, factory=_GuardedConnection)
            connection._catalog_guard = guard
            connection._catalog_path = self.path
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            else:
                guard.__exit__(None, None, None)
            raise

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        secure_directory(self.path.parent)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self.connection(immediate=True) as connection:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"catalog schema {current_version} is newer than supported "
                    f"schema {self.SCHEMA_VERSION}"
                )
            sources_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
            ).fetchone()
            migration_counts: tuple[int, int] | None = None
            if sources_exist is not None and current_version < self.SCHEMA_VERSION:
                migration_counts = self._stage_legacy_catalog(connection)
                self._validate_catalog_lineage(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("legacy catalog foreign key check failed")
                connection.execute(
                    """
                    UPDATE sources
                    SET previous_version_id = NULL
                    WHERE previous_version_id IS NOT NULL
                    """
                )
                self._drop_schema(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    version_id TEXT PRIMARY KEY,
                    created_sequence INTEGER NOT NULL UNIQUE,
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
                    previous_version_id TEXT,
                    FOREIGN KEY (previous_version_id) REFERENCES sources(version_id)
                        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
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
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS sources_before_delete_fts
                BEFORE DELETE ON sources
                BEGIN
                    DELETE FROM source_fts
                    WHERE rowid IN (
                        SELECT fts_rowid FROM source_fts_map
                        WHERE version_id = OLD.version_id
                    );
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS sources_created_sequence_immutable
                BEFORE UPDATE OF created_sequence ON sources
                WHEN NEW.created_sequence <> OLD.created_sequence
                BEGIN
                    SELECT RAISE(ABORT, 'created_sequence is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS sources_source_id_immutable
                BEFORE UPDATE OF source_id ON sources
                WHEN NEW.source_id <> OLD.source_id
                BEGIN
                    SELECT RAISE(ABORT, 'source_id is immutable');
                END
                """
            )
            if migration_counts is not None:
                self._restore_staged_catalog(connection, migration_counts)
            self._repair_fts_orphans(connection)
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @staticmethod
    def _repair_fts_orphans(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM source_fts
            WHERE rowid NOT IN (
                SELECT source_fts_map.fts_rowid
                FROM source_fts_map
                JOIN sources USING (version_id)
            )
            """
        )
        connection.execute(
            """
            DELETE FROM source_fts_map
            WHERE fts_rowid NOT IN (SELECT rowid FROM source_fts)
               OR version_id NOT IN (SELECT version_id FROM sources)
            """
        )

    @staticmethod
    def _stage_legacy_catalog(connection: sqlite3.Connection) -> tuple[int, int]:
        source_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sources)")
        }
        sequence_expression = (
            "created_sequence" if "created_sequence" in source_columns else "rowid"
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE migration_sources AS
            SELECT rowid AS legacy_rowid,
                   {sequence_expression} AS created_sequence,
                   version_id, source_id, space,
                   original_name, relative_path, sha256, media_type, status,
                   previous_version_id
            FROM sources
            """
        )
        fragments_exist = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'source_fragments'
            """
        ).fetchone()
        if fragments_exist is None:
            connection.execute(
                """
                CREATE TEMP TABLE migration_fragments (
                    version_id TEXT, locator TEXT, text TEXT
                )
                """
            )
        else:
            connection.execute(
                """
                CREATE TEMP TABLE migration_fragments AS
                SELECT version_id, locator, text FROM source_fragments
                """
            )
        source_count = connection.execute(
            "SELECT count(*) FROM migration_sources"
        ).fetchone()[0]
        fragment_count = connection.execute(
            "SELECT count(*) FROM migration_fragments"
        ).fetchone()[0]
        if source_count != connection.execute("SELECT count(*) FROM sources").fetchone()[0]:
            raise RuntimeError("legacy source staging count mismatch")
        if fragments_exist is not None and fragment_count != connection.execute(
            "SELECT count(*) FROM source_fragments"
        ).fetchone()[0]:
            raise RuntimeError("legacy fragment staging count mismatch")
        return source_count, fragment_count

    def _restore_staged_catalog(
        self, connection: sqlite3.Connection, expected_counts: tuple[int, int]
    ) -> None:
        connection.execute(
            """
            INSERT INTO sources (
                version_id, created_sequence, source_id, space,
                original_name, relative_path,
                sha256, media_type, status, previous_version_id
            )
            SELECT version_id, created_sequence, source_id, space,
                   original_name, relative_path, sha256, media_type, status,
                   previous_version_id
            FROM migration_sources
            ORDER BY legacy_rowid
            """
        )
        connection.execute(
            """
            INSERT INTO source_fragments (version_id, locator, text)
            SELECT version_id, locator, text FROM migration_fragments
            """
        )
        self._validate_catalog_lineage(connection)
        rows = connection.execute(
            """
            SELECT sources.version_id, sources.source_id, sources.relative_path,
                   sources.space, fragments.locator, fragments.text
            FROM migration_fragments AS fragments
            JOIN sources USING (version_id)
            WHERE trim(fragments.text) <> ''
            """
        ).fetchall()
        for row in rows:
            self._insert_fts_row(
                connection,
                version_id=row["version_id"],
                source_id=row["source_id"],
                relative_path=row["relative_path"],
                locator=row["locator"],
                space=row["space"],
                text=row["text"],
            )
        actual_counts = (
            connection.execute("SELECT count(*) FROM sources").fetchone()[0],
            connection.execute("SELECT count(*) FROM source_fragments").fetchone()[0],
        )
        if actual_counts != expected_counts:
            raise RuntimeError("catalog migration count mismatch")
        indexed_count = len(rows)
        rebuilt_counts = (
            connection.execute("SELECT count(*) FROM source_fts").fetchone()[0],
            connection.execute("SELECT count(*) FROM source_fts_map").fetchone()[0],
        )
        if rebuilt_counts != (indexed_count, indexed_count):
            raise RuntimeError("catalog migration FTS rebuild count mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("catalog migration foreign key check failed")
        connection.execute("DROP TABLE migration_fragments")
        connection.execute("DROP TABLE migration_sources")

    @staticmethod
    def _validate_catalog_lineage(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT version_id, source_id, previous_version_id FROM sources"
        ).fetchall()
        lineage = {
            row["version_id"]: (row["source_id"], row["previous_version_id"])
            for row in rows
        }
        for version_id, (source_id, predecessor_id) in lineage.items():
            seen = {version_id}
            current = predecessor_id
            while current is not None:
                if current in seen:
                    raise ValueError(f"lineage cycle involving {version_id}")
                seen.add(current)
                predecessor = lineage.get(current)
                if predecessor is None:
                    raise ValueError(f"missing predecessor {current}")
                if predecessor[0] != source_id:
                    raise ValueError("lineage predecessor has a different source_id")
                current = predecessor[1]

    @staticmethod
    def _drop_schema(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS source_fts")
        connection.execute("DROP TABLE IF EXISTS source_fts_map")
        connection.execute("DROP TABLE IF EXISTS source_fragments")
        connection.execute("DROP TABLE IF EXISTS sources")

    def _insert_fts_row(
        self,
        connection: sqlite3.Connection,
        *,
        version_id: str,
        source_id: str,
        relative_path: str,
        locator: str,
        space: Space,
        text: str,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO source_fts (
                version_id, source_id, relative_path, locator, space, body
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                source_id,
                relative_path,
                locator,
                space,
                self._searchable_text(text),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_fts_map (fts_rowid, version_id, locator)
            VALUES (?, ?, ?)
            """,
            (cursor.lastrowid, version_id, locator),
        )

    def upsert_source(
        self, source: SourceVersion, fragments: list[tuple[str, str]]
    ) -> None:
        nonblank_fragments = [
            (locator, text) for locator, text in fragments if text.strip()
        ]
        with self.connection(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT created_sequence, source_id
                FROM sources WHERE version_id = ?
                """,
                (source.version_id,),
            ).fetchone()
            if existing is not None:
                if existing["source_id"] != source.source_id:
                    raise ValueError("source_id is immutable for an existing version_id")
                created_sequence = existing["created_sequence"]
                if (
                    source.created_sequence is not None
                    and source.created_sequence != created_sequence
                ):
                    raise ValueError("created_sequence is immutable")
            elif source.created_sequence is not None:
                if isinstance(source.created_sequence, bool) or source.created_sequence <= 0:
                    raise ValueError("created_sequence must be a positive integer")
                created_sequence = source.created_sequence
            else:
                created_sequence = connection.execute(
                    "SELECT coalesce(max(created_sequence), 0) + 1 FROM sources"
                ).fetchone()[0]
            self._validate_source_lineage(connection, source)
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
                    version_id, created_sequence, source_id, space,
                    original_name, relative_path, sha256, media_type, status,
                    previous_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    created_sequence,
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
                self._insert_fts_row(
                    connection,
                    version_id=source.version_id,
                    source_id=source.source_id,
                    relative_path=source.relative_path,
                    locator=locator,
                    space=source.space,
                    text=text,
                )

    @staticmethod
    def _validate_source_lineage(
        connection: sqlite3.Connection, source: SourceVersion
    ) -> None:
        predecessor_id = source.previous_version_id
        if predecessor_id is None:
            return
        if predecessor_id == source.version_id:
            raise ValueError("a source version cannot reference itself as predecessor")
        seen = {source.version_id}
        current = predecessor_id
        while current is not None:
            if current in seen:
                raise ValueError(f"lineage cycle involving {source.version_id}")
            seen.add(current)
            predecessor = connection.execute(
                """
                SELECT source_id, previous_version_id
                FROM sources WHERE version_id = ?
                """,
                (current,),
            ).fetchone()
            if predecessor is None:
                raise ValueError(f"missing predecessor {current}")
            if predecessor["source_id"] != source.source_id:
                raise ValueError("lineage predecessor has a different source_id")
            current = predecessor["previous_version_id"]

    def latest_source(
        self, space: Space, original_name: str
    ) -> SourceVersion | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT candidate.version_id, candidate.created_sequence,
                       candidate.source_id, candidate.space,
                       candidate.original_name, candidate.relative_path,
                       candidate.sha256, candidate.media_type, candidate.status,
                       candidate.previous_version_id
                FROM sources AS candidate
                WHERE candidate.space = ? AND candidate.original_name = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM sources AS successor
                      WHERE successor.previous_version_id = candidate.version_id
                        AND successor.source_id = candidate.source_id
                  )
                ORDER BY candidate.created_sequence DESC
                LIMIT 1
                """,
                (space, original_name),
            ).fetchone()
        return self._source_from_row(row) if row is not None else None

    def source_metadata(
        self,
        version_ids: Collection[str],
        *,
        limit: int = 100,
    ) -> dict[str, dict[str, str]]:
        checked = tuple(dict.fromkeys(version_ids))
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or len(checked) > limit
            or any(not isinstance(item, str) for item in checked)
        ):
            raise ValueError("source metadata request exceeds limit")
        if not checked:
            return {}
        marks = ", ".join("?" for _ in checked)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT version_id, source_id, space,
                       original_name, media_type
                FROM sources
                WHERE version_id IN ({marks})
                """,
                checked,
            ).fetchall()
        return {
            row["version_id"]: {
                "source_id": row["source_id"],
                "space": row["space"],
                "original_name": row["original_name"],
                "media_type": row["media_type"],
            }
            for row in rows
        }

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
        if len(query) > self.MAX_QUERY_CHARACTERS:
            raise ValueError(
                f"query must be at most {self.MAX_QUERY_CHARACTERS} characters"
            )
        if not spaces:
            return []

        terms = self._plain_query_terms(query)
        if len(terms) > self.MAX_QUERY_TERMS:
            raise ValueError(f"query has more than {self.MAX_QUERY_TERMS} terms")
        match_query = self._match_query(terms)
        if not match_query:
            return []
        script_runs = self._script_runs(query)

        placeholders = ", ".join("?" for _ in spaces)
        substring_checks = "".join(
            " AND instr(source_fragments.text, ?) > 0" for _ in script_runs
        )
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
              {substring_checks}
            ORDER BY bm25(source_fts)
            LIMIT ?
        """
        with self.connection() as connection:
            rows = connection.execute(
                sql, (match_query, *spaces, *script_runs, limit)
            ).fetchall()
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
            created_sequence=row["created_sequence"],
        )

    @staticmethod
    def _searchable_text(text: str) -> str:
        """Add bounded n-grams for continuous CJK, Japanese, and Korean text."""
        searchable = [text]
        for run in Catalog._script_runs(text):
            searchable.extend(Catalog._cjk_ngrams(run))
        return " ".join(dict.fromkeys(searchable))

    @staticmethod
    def _cjk_ngrams(text: str) -> list[str]:
        return list(
            dict.fromkeys(
                text[index : index + size]
                for size in range(1, min(3, len(text)) + 1)
                for index in range(len(text) - size + 1)
            )
        )

    @staticmethod
    def _plain_match_query(query: str) -> str:
        return Catalog._match_query(Catalog._plain_query_terms(query))

    @staticmethod
    def _match_query(terms: list[str]) -> str:
        return " AND ".join(f'"{term}"' for term in terms)

    @staticmethod
    def _plain_query_terms(query: str) -> list[str]:
        terms: list[str] = []
        start = 0
        while start < len(query):
            character = query[start]
            script = Catalog._script_kind(character)
            if script is not None:
                end = start + 1
                while end < len(query) and Catalog._script_kind(query[end]) is not None:
                    end += 1
                terms.extend(Catalog._cjk_ngrams(query[start:end]))
                start = end
            elif character.isalnum() or character == "_":
                end = start + 1
                while end < len(query) and (
                    query[end].isalnum() or query[end] == "_"
                ) and Catalog._script_kind(query[end]) is None:
                    end += 1
                terms.append(query[start:end])
                start = end
            else:
                start += 1
        return list(dict.fromkeys(terms))

    @staticmethod
    def _script_kind(character: str) -> str | None:
        if "\u3400" <= character <= "\u9fff":
            return "cjk"
        if "\u3040" <= character <= "\u309f":
            return "hiragana"
        if (
            "\u30a0" <= character <= "\u30ff"
            or "\u31f0" <= character <= "\u31ff"
            or "\uff66" <= character <= "\uff9f"
        ):
            return "katakana"
        if (
            "\u1100" <= character <= "\u11ff"
            or "\u3130" <= character <= "\u318f"
            or "\uac00" <= character <= "\ud7af"
        ):
            return "hangul"
        return None

    @staticmethod
    def _script_runs(text: str) -> list[str]:
        runs: list[str] = []
        start = 0
        while start < len(text):
            script = Catalog._script_kind(text[start])
            if script is None:
                start += 1
                continue
            end = start + 1
            while end < len(text) and Catalog._script_kind(text[end]) is not None:
                end += 1
            runs.append(text[start:end])
            start = end
        return runs
