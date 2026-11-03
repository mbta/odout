import os
import time
from pathlib import Path

import duckdb
import pytest

from odout.server import Catalog, catalog_uri


def write_db(path: Path, views: dict[str, str]) -> None:
    """Write a DuckDB file containing only views, like Odin's fares data repository."""
    path.unlink(missing_ok=True)
    with duckdb.connect(str(path)) as db:
        db.execute("CREATE SCHEMA cubic_ods")
        for name, sql in views.items():
            db.execute(f"CREATE VIEW cubic_ods.{name} AS {sql}")


def bump_mtime(path: Path) -> None:
    """Make sure a rewritten file reports a new last-modified time."""
    mtime = time.time() + 10
    os.utime(path, (mtime, mtime))


@pytest.fixture
def db():
    with duckdb.connect() as connection:
        yield connection


def test_refresh_attaches_catalog(tmp_path, db):
    db_file = tmp_path / "fares.db"
    write_db(db_file, {"a": "SELECT 1 AS x"})

    catalog = Catalog(db, str(db_file))

    assert catalog.refresh() is True
    assert db.execute("SELECT x FROM fares_data_repository.cubic_ods.a").fetchall() == [(1,)]


def test_refresh_skips_unchanged_file(tmp_path, db):
    db_file = tmp_path / "fares.db"
    write_db(db_file, {"a": "SELECT 1 AS x"})
    catalog = Catalog(db, str(db_file))
    catalog.refresh()

    assert catalog.refresh() is False


def test_refresh_picks_up_rewritten_file(tmp_path, db):
    db_file = tmp_path / "fares.db"
    write_db(db_file, {"a": "SELECT 1 AS x"})
    catalog = Catalog(db, str(db_file))
    catalog.refresh()

    # Odin rewrites the file from another process; build it elsewhere and swap it in so this
    # process doesn't open a file it already has attached
    new_file = tmp_path / "new.db"
    write_db(new_file, {"a": "SELECT 1 AS x", "b": "SELECT 2 AS y"})
    new_file.replace(db_file)
    bump_mtime(db_file)

    assert catalog.refresh() is True
    assert db.execute("SELECT y FROM fares_data_repository.cubic_ods.b").fetchall() == [(2,)]


def test_refresh_reattaches_after_failed_attach(tmp_path, db):
    db_file = tmp_path / "fares.db"
    write_db(db_file, {"a": "SELECT 1 AS x"})
    catalog = Catalog(db, str(db_file))
    catalog.refresh()

    # simulate a failed re-attach that left the catalog detached
    db.execute("DETACH fares_data_repository")

    assert catalog.refresh() is True
    assert db.execute("SELECT x FROM fares_data_repository.cubic_ods.a").fetchall() == [(1,)]


def test_refresh_raises_for_missing_file(tmp_path, db):
    catalog = Catalog(db, str(tmp_path / "missing.db"))

    with pytest.raises(Exception):
        catalog.refresh()


def test_catalog_uri_for_prod():
    assert (
        catalog_uri("springboard", "prod")
        == "s3://springboard/odin/data/dictionary/fares_data_repository.db"
    )


def test_catalog_uri_adds_environment_suffix():
    assert (
        catalog_uri("dev-springboard", "dev")
        == "s3://dev-springboard/odin/data/dictionary/fares_data_repository_dev.db"
    )
