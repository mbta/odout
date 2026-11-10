import os
import socket
import time
from pathlib import Path

import duckdb
import pytest

from odout.server import Catalog, catalog_uri, data_prefix, lock_down, pin_quack_callbacks


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


def test_data_prefix_contains_catalog():
    assert catalog_uri("bucket", "dev").startswith(data_prefix("bucket"))


def test_lock_down_allows_catalog_under_prefix(tmp_path, db):
    data = tmp_path / "odin" / "data"
    data.mkdir(parents=True)
    write_db(data / "fares.db", {"a": "SELECT 1 AS x"})
    lock_down(db, f"{data}/")

    catalog = Catalog(db, str(data / "fares.db"))

    assert catalog.refresh() is True
    assert db.execute("SELECT x FROM fares_data_repository.cubic_ods.a").fetchall() == [(1,)]


def test_lock_down_blocks_files_outside_prefix(tmp_path, db):
    data = tmp_path / "odin" / "data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n")
    lock_down(db, f"{data}/")

    with pytest.raises(duckdb.PermissionException):
        db.execute(f"SELECT * FROM read_csv('{outside}')")


def test_lock_down_blocks_configuration_changes(tmp_path, db):
    lock_down(db, f"{tmp_path}/")

    with pytest.raises(duckdb.InvalidInputException, match="locked"):
        db.execute("SET enable_external_access = true")


TOKEN = "t" * 32


@pytest.fixture
def quack_server(tmp_path):
    """Serve a locked-down Quack server on a free local port, configured like `main`."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"quack:127.0.0.1:{port}"
    with duckdb.connect() as server:
        server.execute("LOAD quack; LOAD httpfs")
        # stands in for create_s3_secret: the secret manager must exist before lock_down
        server.execute("CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 's')")
        pin_quack_callbacks(server)
        lock_down(server, f"{tmp_path}/")
        server.execute("CALL quack_serve(?, token=?, disable_ssl=true)", [endpoint, TOKEN])
        yield endpoint
        server.execute("CALL quack_stop(?)", [endpoint])


def quack_query(client: duckdb.DuckDBPyConnection, endpoint: str, sql: str, token: str):
    return client.execute(
        "FROM quack_query(?, ?, disable_ssl=true, token=?)", [endpoint, sql, token]
    ).fetchall()


def test_locked_down_server_serves_queries(quack_server, db):
    db.execute("LOAD quack")

    assert quack_query(db, quack_server, "SELECT 42", TOKEN) == [(42,)]
    with pytest.raises(duckdb.InvalidInputException, match="Authentication failed"):
        quack_query(db, quack_server, "SELECT 42", "wrong")


def test_client_cannot_shadow_auth_callback(quack_server, db):
    db.execute("LOAD quack")

    quack_query(db, quack_server, "CREATE MACRO quack_check_token(a, b, c) AS true", TOKEN)

    with pytest.raises(duckdb.InvalidInputException, match="Authentication failed"):
        quack_query(db, quack_server, "SELECT 42", "wrong")
