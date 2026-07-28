"""Rebuildable local search index for canonical correction records."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .correction_model import CorrectionRecord
from .correction_store import CorrectionStore
from .paths import VaultPaths
from .safety import guarded_catalog_path, secure_directory


class CorrectionIndex:
    SCHEMA_VERSION = 1
    MAX_CANDIDATES = 200
    MAX_TERMS = 64

    def __init__(self, vault: VaultPaths | Path | str) -> None:
        if isinstance(vault, VaultPaths):
            self.paths = vault
        else:
            self.paths = VaultPaths(
                Path(os.path.abspath(os.fspath(vault)))
            )
        self.path = self.paths.correction_index

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS corrections (
                correction_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                space TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                searchable_text TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS correction_fts
            USING fts5(
                correction_id UNINDEXED,
                searchable_text,
                tokenize='unicode61'
            )
            """
        )
        connection.execute(
            f"PRAGMA user_version={CorrectionIndex.SCHEMA_VERSION}"
        )

    @staticmethod
    def _connect_path(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        secure_directory(self.path.parent)
        with guarded_catalog_path(
            self.path,
            allow_missing_main=True,
        ) as bound:
            with closing(self._connect_path(bound)) as connection:
                self._schema(connection)
                connection.commit()

    @staticmethod
    def _searchable(record: CorrectionRecord) -> str:
        applicability = record.applicability
        values = [
            record.correction_rule,
            record.error_type,
            *applicability.file_types,
            *applicability.source_families,
            *applicability.sheet_names,
            *applicability.column_names,
            *applicability.units,
            *applicability.question_types,
            *applicability.keywords,
            *applicability.error_types,
        ]
        return " ".join(values)[:32_000]

    @classmethod
    def _upsert_connection(
        cls,
        connection: sqlite3.Connection,
        record: CorrectionRecord,
    ) -> None:
        searchable = cls._searchable(record)
        space = record.applicability.spaces[0]
        connection.execute(
            "DELETE FROM correction_fts WHERE correction_id = ?",
            (record.correction_id,),
        )
        connection.execute(
            """
            INSERT INTO corrections (
                correction_id, status, space,
                content_sha256, searchable_text
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(correction_id) DO UPDATE SET
                status = excluded.status,
                space = excluded.space,
                content_sha256 = excluded.content_sha256,
                searchable_text = excluded.searchable_text
            """,
            (
                record.correction_id,
                record.status,
                space,
                record.content_sha256,
                searchable,
            ),
        )
        connection.execute(
            """
            INSERT INTO correction_fts (
                correction_id, searchable_text
            )
            VALUES (?, ?)
            """,
            (record.correction_id, searchable),
        )

    def upsert(self, record: CorrectionRecord) -> None:
        self.initialize()
        with guarded_catalog_path(self.path) as bound:
            with closing(self._connect_path(bound)) as connection:
                self._upsert_connection(connection, record)
                connection.commit()

    def rebuild(self, store: CorrectionStore) -> int:
        records, truncated = store.iter_records()
        if truncated:
            raise ValueError("canonical correction scan was truncated")
        secure_directory(self.path.parent)
        temporary = self.path.parent / (
            f".corrections-{uuid4().hex}.sqlite3"
        )
        try:
            with closing(self._connect_path(temporary)) as connection:
                self._schema(connection)
                for record in records:
                    self._upsert_connection(connection, record)
                connection.commit()
                if (
                    connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                    != "ok"
                ):
                    raise ValueError(
                        "rebuilt correction index failed integrity check"
                    )
                connection.execute("PRAGMA journal_mode=DELETE")
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            with guarded_catalog_path(
                self.path,
                allow_missing_main=True,
            ):
                pass
            os.replace(temporary, self.path)
            if not self.integrity_check():
                raise ValueError("published correction index is invalid")
            return len(records)
        finally:
            temporary.unlink(missing_ok=True)
            Path(f"{temporary}-wal").unlink(missing_ok=True)
            Path(f"{temporary}-shm").unlink(missing_ok=True)
            Path(f"{temporary}-journal").unlink(missing_ok=True)

    @staticmethod
    def _match_query(terms: tuple[str, ...]) -> str:
        safe = []
        for term in terms:
            cleaned = term.strip().replace('"', '""')
            if cleaned:
                safe.append(f'"{cleaned}"')
        return " OR ".join(safe)

    def candidates(
        self,
        *,
        space: str,
        terms: tuple[str, ...],
        limit: int = 100,
    ) -> tuple[list[str], bool]:
        if (
            not isinstance(space, str)
            or not space
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.MAX_CANDIDATES
            or len(terms) > self.MAX_TERMS
            or any(not isinstance(term, str) for term in terms)
        ):
            raise ValueError("correction candidate query is invalid")
        query = self._match_query(terms)
        with guarded_catalog_path(self.path) as bound:
            with closing(self._connect_path(bound)) as connection:
                version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                if version != self.SCHEMA_VERSION:
                    raise ValueError("correction index schema is invalid")
                if query:
                    rows = connection.execute(
                        """
                        SELECT c.correction_id
                        FROM correction_fts AS f
                        JOIN corrections AS c
                          ON c.correction_id = f.correction_id
                        WHERE correction_fts MATCH ?
                          AND c.space = ?
                          AND c.status = 'active'
                        ORDER BY bm25(correction_fts), c.correction_id
                        LIMIT ?
                        """,
                        (query, space, limit + 1),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT correction_id
                        FROM corrections
                        WHERE space = ? AND status = 'active'
                        ORDER BY correction_id
                        LIMIT ?
                        """,
                        (space, limit + 1),
                    ).fetchall()
        return (
            [row["correction_id"] for row in rows[:limit]],
            len(rows) > limit,
        )

    def integrity_check(self) -> bool:
        try:
            with guarded_catalog_path(self.path) as bound:
                with closing(self._connect_path(bound)) as connection:
                    if (
                        connection.execute(
                            "PRAGMA integrity_check"
                        ).fetchone()[0]
                        != "ok"
                    ):
                        return False
                    if (
                        connection.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0]
                        != self.SCHEMA_VERSION
                    ):
                        return False
                    counts = connection.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM corrections),
                          (SELECT count(*) FROM correction_fts)
                        """
                    ).fetchone()
                    return counts[0] == counts[1]
        except (OSError, ValueError, sqlite3.Error):
            return False
