import pytest


def test_search_returns_indexed_traditional_chinese_source(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.models import SourceVersion

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    source = SourceVersion(
        source_id="source-1",
        version_id="version-1",
        space="work",
        original_name="notes.md",
        relative_path="notes.md",
        sha256="a" * 64,
        media_type="text/markdown",
        status="published",
    )

    catalog.upsert_source(source, [("line:1", "卡帕西的持續累積知識庫")])

    hits = catalog.search("累積知識庫", {"work"})

    assert [hit.version_id for hit in hits] == ["version-1"]
    assert hits[0].space == "work"
    assert hits[0].score > 0


def test_cjk_ngram_candidates_require_the_original_contiguous_run(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", "累積知，積知識，知識庫")]
    )

    assert catalog.search("累積知識庫", {"work"}) == []


def test_search_finds_japanese_substring_inside_continuous_text(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", "これは継続的な知識ベースです")]
    )

    assert [hit.version_id for hit in catalog.search("知識ベース", {"work"})] == [
        "version-1"
    ]


def test_japanese_mixed_script_query_requires_full_continuity(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(version_id="separated"),
        [("line:1", "東京の観光地にタワーがあります")],
    )
    catalog.upsert_source(
        make_source(
            source_id="source-2", version_id="contiguous", sha256="b" * 64
        ),
        [("line:1", "東京タワーがあります")],
    )

    assert [
        hit.version_id for hit in catalog.search("東京タワー", {"work"})
    ] == ["contiguous"]


def test_initialize_rebuilds_v3_index_for_mixed_script_ngrams(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", "東京タワーがあります")]
    )
    with catalog.connection() as connection:
        rowid = connection.execute(
            "SELECT fts_rowid FROM source_fts_map WHERE version_id = ?",
            ("version-1",),
        ).fetchone()[0]
        connection.execute(
            "UPDATE source_fts SET body = ? WHERE rowid = ?",
            ("東京 タワー 東 京 タ ワ ー 東京 タワ ワー", rowid),
        )
        connection.execute("PRAGMA user_version=3")

    catalog.initialize()

    assert [hit.version_id for hit in catalog.search("東京タワー", {"work"})] == [
        "version-1"
    ]


def test_v3_to_v4_migration_preserves_self_fk_lineage(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(version_id="old"), [("line:1", "old text")]
    )
    catalog.upsert_source(
        make_source(
            version_id="new", sha256="b" * 64, previous_version_id="old"
        ),
        [("line:1", "new searchable text")],
    )
    with catalog.connection() as connection:
        connection.execute("PRAGMA user_version=3")

    catalog.initialize()

    latest = catalog.latest_source("work", "notes.md")
    assert latest is not None
    assert latest.version_id == "new"
    assert latest.previous_version_id == "old"
    assert [hit.version_id for hit in catalog.search("searchable", {"work"})] == [
        "new"
    ]


def test_search_finds_korean_substring_inside_continuous_text(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", "지속적으로축적되는지식베이스")]
    )

    assert [hit.version_id for hit in catalog.search("지식베이스", {"work"})] == [
        "version-1"
    ]


def test_cjk_ngrams_are_linearly_bounded_for_1000_characters():
    from local_kb.catalog import Catalog

    text = "知" * 1000

    terms = Catalog._cjk_ngrams(text)
    indexed_text = Catalog._searchable_text(text)

    assert len(terms) <= len(text) * 3
    assert sum(map(len, terms)) <= len(text) * 6
    assert len(indexed_text.split()) <= len(text) * 3 + 1
    assert len(indexed_text) <= len(text) * 10


def test_repeated_script_ngrams_are_deduplicated():
    from local_kb.catalog import Catalog

    assert Catalog._cjk_ngrams("知" * 20) == ["知", "知知", "知知知"]


def test_plain_query_terms_are_deduplicated():
    from local_kb.catalog import Catalog

    assert Catalog._plain_match_query("alpha alpha") == '"alpha"'


def test_connection_context_commits_and_closes_database(tmp_path):
    import sqlite3
    import pytest
    from local_kb.catalog import Catalog

    database = tmp_path / "catalog.sqlite3"
    catalog = Catalog(database)
    catalog.initialize()

    with catalog.connection() as connection:
        connection.execute(
            "INSERT INTO sources "
            "(version_id, created_sequence, source_id, space, original_name, "
            "relative_path, sha256, media_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("v1", 1, "s1", "work", "a.md", "a.md", "b" * 64, "text/plain", "archived"),
        )

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    database.unlink()
    catalog.initialize()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_catalog_rejects_hardlinked_database_or_sidecar_without_modifying_alias(
    tmp_path, suffix
):
    import os
    from pathlib import Path

    import pytest

    from local_kb.catalog import Catalog

    database = tmp_path / "catalog.sqlite3"
    catalog = Catalog(database)
    catalog.initialize()
    candidate = Path(f"{database}{suffix}")
    if candidate.exists():
        candidate.unlink()
    outside = tmp_path / f"outside{suffix or '-main'}"
    outside.write_bytes(b"keep-external-bytes")
    os.link(outside, candidate)
    before = outside.read_bytes()

    with pytest.raises(ValueError, match="catalog.*unsafe|single-link"):
        catalog.initialize()

    assert outside.read_bytes() == before


def test_connection_context_rolls_back_on_error(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(RuntimeError, match="abort"):
        with catalog.connection() as connection:
            connection.execute(
                "INSERT INTO sources "
                "(version_id, created_sequence, source_id, space, original_name, "
                "relative_path, sha256, media_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("v1", 1, "s1", "work", "a.md", "a.md", "b" * 64, "text/plain", "archived"),
            )
            raise RuntimeError("abort")

    with catalog.connection() as connection:
        count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]

    assert count == 0


def test_initialize_migrates_v1_catalog_without_losing_sources_or_lineage(tmp_path):
    import sqlite3

    from local_kb.catalog import Catalog

    database = tmp_path / "catalog.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.execute(
        """
        CREATE TABLE sources (
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
    legacy.execute(
        """
        CREATE TABLE source_fragments (
            version_id TEXT NOT NULL,
            locator TEXT NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (version_id, locator)
        )
        """
    )
    legacy.execute(
        """
        CREATE VIRTUAL TABLE source_fts USING fts5(
            version_id UNINDEXED, source_id UNINDEXED,
            relative_path UNINDEXED, locator UNINDEXED,
            space UNINDEXED, body, tokenize='unicode61'
        )
        """
    )
    legacy.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "s1", "work", "old.md", "old.md", "a" * 64, "text/plain", "archived", None),
    )
    legacy.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("new", "s1", "work", "old.md", "old.md", "b" * 64, "text/plain", "published", "old"),
    )
    legacy.executemany(
        "INSERT INTO source_fragments VALUES (?, ?, ?)",
        [("old", "line:1", "legacy old text"), ("new", "line:1", "legacy searchable text")],
    )
    legacy.execute("PRAGMA user_version=1")
    legacy.commit()
    legacy.close()

    catalog = Catalog(database)
    catalog.initialize()

    with catalog.connection() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        source_count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]
        fragment_count = connection.execute(
            "SELECT count(*) FROM source_fragments"
        ).fetchone()[0]
        mapping_count = connection.execute(
            "SELECT count(*) FROM source_fts_map"
        ).fetchone()[0]
        predecessor = connection.execute(
            "SELECT previous_version_id FROM sources WHERE version_id = 'new'"
        ).fetchone()[0]
        sequences = [
            tuple(row)
            for row in connection.execute(
                "SELECT version_id, created_sequence FROM sources "
                "ORDER BY created_sequence"
            )
        ]

    assert version == Catalog.SCHEMA_VERSION
    assert source_count == 2
    assert fragment_count == 2
    assert mapping_count == 2
    assert predecessor == "old"
    assert sequences == [("old", 1), ("new", 2)]
    assert catalog.latest_source("work", "old.md").version_id == "new"
    assert [hit.version_id for hit in catalog.search("searchable", {"work"})] == [
        "new"
    ]


def test_failed_v1_migration_rolls_back_to_intact_legacy_catalog(tmp_path):
    import sqlite3

    import pytest

    from local_kb.catalog import Catalog

    database = tmp_path / "catalog.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.execute(
        """
        CREATE TABLE sources (
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
    legacy.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "s1", "work", "old.md", "old.md", "a" * 64, "text/plain", "invalid", None),
    )
    legacy.execute("PRAGMA user_version=1")
    legacy.commit()
    legacy.close()

    with pytest.raises(sqlite3.IntegrityError):
        Catalog(database).initialize()

    legacy = sqlite3.connect(database)
    preserved = legacy.execute(
        "SELECT version_id, status FROM sources"
    ).fetchall()
    version = legacy.execute("PRAGMA user_version").fetchone()[0]
    legacy.close()

    assert preserved == [("legacy", "invalid")]
    assert version == 1


def test_every_connection_enables_foreign_keys(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with catalog.connection() as connection:
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert enabled == 1


def test_duplicate_hash_for_another_version_is_rejected_without_orphans(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(version_id="version-1", sha256="b" * 64),
        [("line:1", "first version")],
    )

    with pytest.raises(ValueError, match="sha256"):
        catalog.upsert_source(
            make_source(version_id="version-2", sha256="b" * 64),
            [("line:2", "second version")],
        )

    with catalog.connection() as connection:
        versions = [
            row["version_id"]
            for row in connection.execute("SELECT version_id FROM sources")
        ]
        fts_versions = [
            row["version_id"]
            for row in connection.execute("SELECT version_id FROM source_fts")
        ]

    assert versions == ["version-1"]
    assert fts_versions == ["version-1"]


def test_deleting_source_cascades_to_fragments(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "fragment")])

    with catalog.connection() as connection:
        connection.execute("DELETE FROM sources WHERE version_id = ?", ("version-1",))
        remaining = connection.execute(
            "SELECT count(*) FROM source_fragments"
        ).fetchone()[0]

    assert remaining == 0


def test_delete_then_reinsert_cannot_match_orphaned_fts_text(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    source = make_source()
    catalog.upsert_source(source, [("line:1", "stale orphan term")])

    with catalog.connection() as connection:
        connection.execute("DELETE FROM sources WHERE version_id = ?", ("version-1",))

    catalog.upsert_source(source, [("line:1", "fresh replacement term")])

    assert catalog.search("stale", {"work"}) == []
    assert [hit.text for hit in catalog.search("fresh", {"work"})] == [
        "fresh replacement term"
    ]


def test_initialize_repairs_unmapped_fts_orphans(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    with catalog.connection() as connection:
        orphan_rowid = connection.execute(
            """
            INSERT INTO source_fts (
                version_id, source_id, relative_path, locator, space, body
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("ghost", "ghost", "ghost.md", "line:1", "work", "orphan"),
        ).lastrowid

    catalog.initialize()

    with catalog.connection() as connection:
        remaining = connection.execute(
            "SELECT count(*) FROM source_fts WHERE rowid = ?", (orphan_rowid,)
        ).fetchone()[0]

    assert remaining == 0


def test_search_preserves_each_fragment_locator(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(),
        [("page:1", "shared term first"), ("page:2", "shared term second")],
    )

    hits = catalog.search("shared", {"work"})

    assert {hit.locator for hit in hits} == {"page:1", "page:2"}
    assert {hit.text for hit in hits} == {"shared term first", "shared term second"}


def test_search_excludes_sources_outside_requested_space(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(version_id="work-version", sha256="b" * 64),
        [("line:1", "isolated result")],
    )
    catalog.upsert_source(
        make_source(
            version_id="personal-version", space="personal", sha256="c" * 64
        ),
        [("line:1", "isolated result")],
    )

    hits = catalog.search("isolated", {"work"})

    assert [hit.version_id for hit in hits] == ["work-version"]
    assert hits[0].space == "work"


def test_reupsert_removes_stale_fragments(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    source = make_source()
    catalog.upsert_source(source, [("line:1", "stale content")])
    catalog.upsert_source(source, [("line:2", "fresh content")])

    assert catalog.search("stale", {"work"}) == []
    assert [hit.locator for hit in catalog.search("fresh", {"work"})] == ["line:2"]


def test_fts_rowids_have_an_indexed_version_mapping(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "mapped fragment")])

    with catalog.connection() as connection:
        mapping = connection.execute(
            "SELECT version_id, locator FROM source_fts_map"
        ).fetchone()
        indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(source_fts_map)")
        }

    assert tuple(mapping) == ("version-1", "line:1")
    assert "ix_source_fts_map_version_id" in indexes


def test_fts_delete_plan_uses_rowid_mapping_index(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with catalog.connection() as connection:
        plan = [
            row["detail"]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "DELETE FROM source_fts WHERE rowid IN "
                "(SELECT fts_rowid FROM source_fts_map WHERE version_id = ?)",
                ("version-1",),
            )
        ]

    assert any("ix_source_fts_map_version_id" in detail for detail in plan)
    assert any("VIRTUAL TABLE INDEX 0:=" in detail for detail in plan)


def test_reupsert_keeps_one_mapped_fts_row_per_version_at_scale(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    for number in range(100):
        catalog.upsert_source(
            make_source(
                source_id=f"source-{number}",
                version_id=f"version-{number}",
                original_name=f"note-{number}.md",
                sha256=f"{number:064x}",
            ),
            [("line:1", f"common fragment {number}")],
        )

    catalog.upsert_source(
        make_source(
            source_id="source-50",
            version_id="version-50",
            original_name="note-50.md",
            sha256=f"{50:064x}",
        ),
        [("line:2", "replacement fragment")],
    )

    with catalog.connection() as connection:
        mapped_count = connection.execute(
            "SELECT count(*) FROM source_fts_map"
        ).fetchone()[0]

    assert mapped_count == 100
    assert "version-50" not in {
        hit.version_id for hit in catalog.search("common", {"work"}, limit=100)
    }
    assert catalog.search("replacement", {"work"})[0].locator == "line:2"


def test_upsert_ignores_blank_fragments(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", " \n\t "), ("line:2", "usable fragment")]
    )

    hits = catalog.search("usable", {"work"})

    assert [hit.locator for hit in hits] == ["line:2"]


def test_fts_rows_store_the_source_space(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(space="work"), [("line:1", "space stored")])

    with catalog.connection() as connection:
        row = connection.execute(
            "SELECT space FROM source_fts WHERE version_id = ?", ("version-1",)
        ).fetchone()

    assert row is not None
    assert row["space"] == "work"


def test_fts_schema_marks_space_unindexed(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with catalog.connection() as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'source_fts'"
        ).fetchone()["sql"]

    assert "space UNINDEXED" in schema


def test_upsert_does_not_store_blank_fragments(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(), [("line:1", " \n\t "), ("line:2", "usable fragment")]
    )

    with catalog.connection() as connection:
        stored_locators = {
            table: [
                row["locator"]
                for row in connection.execute(f"SELECT locator FROM {table}")
            ]
            for table in ("source_fragments", "source_fts")
        }

    assert stored_locators == {
        "source_fragments": ["line:2"],
        "source_fts": ["line:2"],
    }


def test_latest_source_returns_newest_matching_original_name(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(version_id="old", sha256="d" * 64), [("line:1", "old")]
    )
    catalog.upsert_source(
        make_source(version_id="new", sha256="e" * 64),
        [("line:1", "new")],
    )
    catalog.upsert_source(
        make_source(
            version_id="other", original_name="other.md", sha256="f" * 64
        ),
        [("line:1", "other")],
    )

    latest = catalog.latest_source("work", "notes.md")

    assert latest is not None
    assert latest.version_id == "new"


def test_latest_source_does_not_move_back_when_old_version_is_retried(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    old = make_source(version_id="old", sha256="d" * 64)
    new = make_source(
        version_id="new", sha256="e" * 64, previous_version_id="old"
    )
    catalog.upsert_source(old, [("line:1", "old")])
    catalog.upsert_source(new, [("line:1", "new")])

    catalog.upsert_source(old, [("line:2", "old retry")])

    assert catalog.latest_source("work", "notes.md").version_id == "new"


def test_latest_source_is_stable_during_shuffled_rebuild(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    old = make_source(version_id="old", sha256="d" * 64, created_sequence=1)
    new = make_source(version_id="new", sha256="e" * 64, created_sequence=2)

    catalog.upsert_source(new, [("line:1", "new")])
    catalog.upsert_source(old, [("line:1", "old")])

    assert catalog.latest_source("work", "notes.md").version_id == "new"


def test_upsert_retry_preserves_immutable_created_sequence(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    source = make_source(created_sequence=41)
    catalog.upsert_source(source, [("line:1", "first")])
    catalog.upsert_source(
        make_source(created_sequence=None), [("line:2", "retry")]
    )

    with catalog.connection() as connection:
        sequence = connection.execute(
            "SELECT created_sequence FROM sources WHERE version_id = ?",
            ("version-1",),
        ).fetchone()[0]

    assert sequence == 41


def test_database_rejects_created_sequence_mutation(tmp_path):
    import sqlite3

    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(created_sequence=41), [])

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with catalog.connection() as connection:
            connection.execute(
                "UPDATE sources SET created_sequence = 42 WHERE version_id = ?",
                ("version-1",),
            )

    assert catalog.latest_source("work", "notes.md").created_sequence == 41


def test_upsert_rejects_source_id_mutation_without_damaging_lineage(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(version_id="old"), [])
    catalog.upsert_source(
        make_source(
            version_id="new", sha256="b" * 64, previous_version_id="old"
        ),
        [],
    )

    with pytest.raises(ValueError, match="source_id.*immutable"):
        catalog.upsert_source(
            make_source(
                source_id="changed-source", version_id="old", sha256="a" * 64
            ),
            [],
        )

    with catalog.connection() as connection:
        lineage = [
            tuple(row)
            for row in connection.execute(
                "SELECT version_id, source_id, previous_version_id "
                "FROM sources ORDER BY created_sequence"
            )
        ]

    assert lineage == [
        ("old", "source-1", None),
        ("new", "source-1", "old"),
    ]
    assert catalog.latest_source("work", "notes.md").version_id == "new"


def test_database_rejects_raw_source_id_mutation(tmp_path):
    import sqlite3

    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [])

    with pytest.raises(sqlite3.IntegrityError, match="source_id.*immutable"):
        with catalog.connection() as connection:
            connection.execute(
                "UPDATE sources SET source_id = ? WHERE version_id = ?",
                ("changed-source", "version-1"),
            )

    assert catalog.latest_source("work", "notes.md").source_id == "source-1"


def test_upsert_rejects_dangling_predecessor(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(ValueError, match="predecessor"):
        catalog.upsert_source(
            make_source(version_id="new", previous_version_id="missing"), []
        )


def test_upsert_rejects_cross_source_predecessor(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(version_id="old"), [])

    with pytest.raises(ValueError, match="source_id"):
        catalog.upsert_source(
            make_source(
                source_id="another-source",
                version_id="new",
                sha256="b" * 64,
                previous_version_id="old",
            ),
            [],
        )


def test_upsert_rejects_self_predecessor(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(ValueError, match="itself"):
        catalog.upsert_source(
            make_source(previous_version_id="version-1"), []
        )


def test_upsert_rejects_lineage_cycle(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(version_id="old"), [])
    catalog.upsert_source(
        make_source(
            version_id="new", sha256="b" * 64, previous_version_id="old"
        ),
        [],
    )

    with pytest.raises(ValueError, match="cycle"):
        catalog.upsert_source(
            make_source(
                version_id="old", sha256="a" * 64, previous_version_id="new"
            ),
            [],
        )


def test_deleting_predecessor_cannot_create_dangling_lineage(tmp_path):
    import sqlite3

    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(version_id="old"), [])
    catalog.upsert_source(
        make_source(
            version_id="new", sha256="b" * 64, previous_version_id="old"
        ),
        [],
    )

    with pytest.raises(sqlite3.IntegrityError):
        with catalog.connection() as connection:
            connection.execute("DELETE FROM sources WHERE version_id = 'old'")

    assert catalog.latest_source("work", "notes.md").version_id == "new"


def test_latest_ignores_cross_source_successor_in_corrupt_legacy_data(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    with catalog.connection() as connection:
        connection.executemany(
            """
            INSERT INTO sources (
                version_id, created_sequence, source_id, space, original_name,
                relative_path, sha256, media_type, status, previous_version_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("old", 1, "source-a", "work", "a.md", "a.md", "a" * 64, "text/plain", "archived", None),
                ("other", 2, "source-b", "work", "b.md", "b.md", "b" * 64, "text/plain", "archived", "old"),
            ],
        )

    assert catalog.latest_source("work", "a.md").version_id == "old"


def test_search_with_no_spaces_returns_no_hits(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "visible when allowed")])

    assert catalog.search("visible", set()) == []


def test_search_with_blank_query_returns_no_hits(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "alpha")])

    assert catalog.search(" \n\t ", {"work"}) == []


def test_search_treats_fts_punctuation_as_plain_text(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "alpha")])

    assert catalog.search('"alpha', {"work"})
    assert catalog.search("-alpha", {"work"})
    assert catalog.search("alpha OR", {"work"}) == []


def test_search_rejects_string_instead_of_space_collection(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(TypeError, match="spaces"):
        catalog.search("alpha", "work")


def test_search_requires_a_bounded_positive_limit(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    for invalid_limit in (0, -1, 101, True):
        with pytest.raises(ValueError, match="limit"):
            catalog.search("alpha", {"work"}, limit=invalid_limit)


def test_search_rejects_oversized_repeated_cjk_query(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(ValueError, match="query"):
        catalog.search("知" * 500, {"work"})


def test_search_rejects_too_many_distinct_terms(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    query = " ".join(f"t{number}" for number in range(65))

    with pytest.raises(ValueError, match="terms"):
        catalog.search(query, {"work"})


def test_jobs_do_not_share_metadata_instances():
    from local_kb.models import Job

    first = Job(job_id="one", source_path="one.md")
    second = Job(job_id="two", source_path="two.md")
    first.metadata["owner"] = "first"

    assert second.metadata == {}
    assert first.to_dict()["metadata"] == {"owner": "first"}


def test_source_status_is_separate_and_includes_pending_extractor():
    from typing import get_args, get_type_hints

    from local_kb.models import SourceStatus, SourceVersion

    assert "pending_extractor" in get_args(SourceStatus)
    assert get_type_hints(SourceVersion)["status"] is SourceStatus


def test_pending_extractor_status_round_trips_through_catalog(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        make_source(status="pending_extractor"), [("line:1", "metadata only")]
    )

    restored = catalog.latest_source("work", "notes.md")

    assert restored is not None
    assert restored.status == "pending_extractor"


def test_database_rejects_unknown_source_status(tmp_path):
    import sqlite3

    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        catalog.upsert_source(make_source(status="invented"), [])


def make_source(
    *,
    source_id="source-1",
    version_id="version-1",
    space="work",
    original_name="notes.md",
    sha256="a" * 64,
    status="published",
    previous_version_id=None,
    created_sequence=None,
):
    from local_kb.models import SourceVersion

    return SourceVersion(
        source_id=source_id,
        version_id=version_id,
        space=space,
        original_name=original_name,
        relative_path=original_name,
        sha256=sha256,
        media_type="text/markdown",
        status=status,
        previous_version_id=previous_version_id,
        created_sequence=created_sequence,
    )
