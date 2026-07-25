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


def test_cjk_ngrams_are_linearly_bounded_for_1000_characters():
    from local_kb.catalog import Catalog

    text = "知" * 1000

    terms = Catalog._cjk_ngrams(text)
    indexed_text = Catalog._searchable_text(text)

    assert len(terms) <= len(text) * 3
    assert sum(map(len, terms)) <= len(text) * 6
    assert len(indexed_text.split()) <= len(text) * 3 + 1
    assert len(indexed_text) <= len(text) * 10


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
            "(version_id, source_id, space, original_name, relative_path, sha256, "
            "media_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("v1", "s1", "work", "a.md", "a.md", "b" * 64, "text/plain", "archived"),
        )

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    database.unlink()
    catalog.initialize()


def test_connection_context_rolls_back_on_error(tmp_path):
    import pytest

    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with pytest.raises(RuntimeError, match="abort"):
        with catalog.connection() as connection:
            connection.execute(
                "INSERT INTO sources "
                "(version_id, source_id, space, original_name, relative_path, sha256, "
                "media_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("v1", "s1", "work", "a.md", "a.md", "b" * 64, "text/plain", "archived"),
            )
            raise RuntimeError("abort")

    with catalog.connection() as connection:
        count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]

    assert count == 0


def test_initialize_rebuilds_legacy_catalog_schema(tmp_path):
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
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "s1", "work", "old.md", "old.md", "a" * 64, "text/plain", "archived", None),
    )
    legacy.commit()
    legacy.close()

    catalog = Catalog(database)
    catalog.initialize()

    with catalog.connection() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        source_count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]
        map_exists = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'source_fts_map'"
        ).fetchone()[0]

    assert version == Catalog.SCHEMA_VERSION
    assert source_count == 0
    assert map_exists == 1


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
    old = make_source(version_id="old", sha256="d" * 64)
    new = make_source(
        version_id="new", sha256="e" * 64, previous_version_id="old"
    )

    catalog.upsert_source(new, [("line:1", "new")])
    catalog.upsert_source(old, [("line:1", "old")])

    assert catalog.latest_source("work", "notes.md").version_id == "new"


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
    )
