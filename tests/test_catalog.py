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

    with catalog.connect() as connection:
        row = connection.execute(
            "SELECT space FROM source_fts WHERE version_id = ?", ("version-1",)
        ).fetchone()

    assert row is not None
    assert row["space"] == "work"


def test_fts_schema_marks_space_unindexed(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()

    with catalog.connect() as connection:
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

    with catalog.connect() as connection:
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
        make_source(version_id="new", sha256="e" * 64), [("line:1", "new")]
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


def test_search_with_no_spaces_returns_no_hits(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(make_source(), [("line:1", "visible when allowed")])

    assert catalog.search("visible", set()) == []


def test_jobs_do_not_share_metadata_instances():
    from local_kb.models import Job

    first = Job(job_id="one", source_path="one.md")
    second = Job(job_id="two", source_path="two.md")
    first.metadata["owner"] = "first"

    assert second.metadata == {}
    assert first.to_dict()["metadata"] == {"owner": "first"}


def make_source(
    *,
    version_id="version-1",
    space="work",
    original_name="notes.md",
    sha256="a" * 64,
):
    from local_kb.models import SourceVersion

    return SourceVersion(
        source_id="source-1",
        version_id=version_id,
        space=space,
        original_name=original_name,
        relative_path=original_name,
        sha256=sha256,
        media_type="text/markdown",
        status="published",
    )
