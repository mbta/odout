"""Serve the Odin fares data repository DuckDB catalog to remote clients over Quack."""

import os
import signal
import threading

import duckdb

EXTENSION_DIRECTORY = "/opt/duckdb/extensions"
# spill to the task's ephemeral storage; memory_limit keeps DuckDB's default of 80% of task memory
TEMP_DIRECTORY = "/home/quack/duckdb_tmp"
CATALOG_ALIAS = "fares_data_repository"
# Odin's data prefix (odin.utils.locations.ODIN_DATA); every catalog view reads beneath it
ODIN_DATA = "odin/data"
# where Odin's data dictionary job writes the catalog file (odin.utils.locations.ODIN_DICTIONARY)
ODIN_DICTIONARY = f"{ODIN_DATA}/dictionary"
DB_FILE = "fares_data_repository.db"
QUACK_ENDPOINT = "quack:0.0.0.0:9494"
DEFAULT_REFRESH_SECONDS = 300


def log(message: str) -> None:
    """Print a log line, flushed so it reaches the container log immediately."""
    print(message, flush=True)


def catalog_uri(springboard: str, aws_env: str) -> str:
    """
    S3 URI of the catalog file for an environment.

    Mirrors Odin's naming: production uses the plain file name, other environments add
    an `_<env>` suffix.
    """
    db_file = DB_FILE
    if aws_env.lower() != "prod":
        db_file = db_file.replace(".db", f"_{aws_env}.db")
    return f"s3://{springboard}/{ODIN_DICTIONARY}/{db_file}"


def data_prefix(springboard: str) -> str:
    """S3 prefix holding the catalog file and all the data its views read."""
    return f"s3://{springboard}/{ODIN_DATA}/"


def pin_quack_callbacks(db: duckdb.DuckDBPyConnection) -> None:
    """
    Point Quack's auth callbacks at the built-ins by fully qualified name.

    The default settings name the callbacks unqualified, so a client holding the token could
    `CREATE MACRO quack_check_token(...) AS true` and shadow the built-in, opening the server
    to every token. Configuration locking does not prevent that; qualifying the names does.
    """
    db.execute("SET quack_authentication_function = 'system.main.quack_check_token'")
    db.execute("SET quack_authorization_function = 'system.main.quack_nop_authorization'")


def lock_down(db: duckdb.DuckDBPyConnection, allowed_prefix: str) -> None:
    """
    Restrict what queries on `db`, including remote Quack clients, can reach or change.

    File access (local disk, S3, other URLs) is limited to `allowed_prefix`, and
    configuration is frozen so clients can't undo this or swap the Quack auth callbacks.
    The temp directory stays usable for spilling. Extensions must already be loaded and a
    secret already created: afterwards neither can be installed or initialized from disk,
    and without an initialized secret manager every Quack request fails with HTTP 500.

    `allowed_prefix` is a string-prefix check, so `..` segments in a URL can pass it;
    the task role's S3 policy is the real boundary.
    """
    quoted_prefix = allowed_prefix.replace("'", "''")
    db.execute(f"SET allowed_directories = ['{quoted_prefix}']")
    db.execute("SET enable_external_access = false")
    db.execute("SET lock_configuration = true")


class Catalog:
    """
    Read-only attachment of the fares data repository database file.

    Odin periodically rewrites the file in place, so `refresh` re-attaches it whenever the
    object's last-modified time changes. Queries already running against the old attachment
    complete normally after it is detached.
    """

    def __init__(self, db: duckdb.DuckDBPyConnection, uri: str, alias: str = CATALOG_ALIAS):
        """Track the database file at `uri`, attached on `db` as `alias`."""
        self.db = db
        self.uri = uri
        self.alias = alias
        self.last_modified: str | None = None

    def _remote_last_modified(self) -> str:
        # cast to VARCHAR so the python client doesn't need pytz to return a TIMESTAMPTZ
        result = self.db.execute(
            "SELECT last_modified::VARCHAR FROM read_blob(?)", [self.uri]
        ).fetchone()
        if result is None:
            raise FileNotFoundError(self.uri)
        return result[0]

    def _is_attached(self) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM duckdb_databases() WHERE database_name = ?", [self.alias]
            ).fetchone()
            is not None
        )

    def refresh(self) -> bool:
        """Attach the database file, or re-attach it if it changed. Return True if attached."""
        last_modified = self._remote_last_modified()
        if last_modified == self.last_modified and self._is_attached():
            return False

        self.db.execute(f"DETACH DATABASE IF EXISTS {self.alias}")
        # ATTACH doesn't accept a bound parameter for its path
        quoted_uri = self.uri.replace("'", "''")
        self.db.execute(f"ATTACH '{quoted_uri}' AS {self.alias} (READ_ONLY)")
        self.last_modified = last_modified
        log(f"attached {self.uri} as {self.alias} (last modified {last_modified})")
        return True


def create_s3_secret(db: duckdb.DuckDBPyConnection) -> None:
    """
    Create the DuckDB S3 secret from the AWS default credential chain.

    On ECS this resolves the task role's container credentials. REFRESH auto is required
    because those credentials expire while the server is running.
    """
    db.execute(
        f"""
        CREATE OR REPLACE SECRET s3_task_role (
            TYPE s3,
            PROVIDER credential_chain,
            REFRESH auto,
            REGION '{os.environ.get("AWS_REGION", "us-east-1")}'
        )
        """
    )


def main() -> None:
    """Attach the catalog, serve it over Quack, and refresh it until SIGTERM."""
    token = os.environ.pop("QUACK_TOKEN", "")
    if len(token.encode()) < 32:
        raise SystemExit(
            "QUACK_TOKEN must contain at least 32 bytes; inject a strong random secret"
        )

    springboard = os.environ.get("DATA_SPRINGBOARD", "")
    task_group = os.environ.get("ECS_TASK_GROUP", "")
    if not springboard or not task_group:
        raise SystemExit("DATA_SPRINGBOARD and ECS_TASK_GROUP must be set")
    # same environment derivation as Odin, e.g. "family:odout-dev" -> "dev"
    uri = catalog_uri(springboard, task_group.split("-")[-1])
    refresh_seconds = int(os.environ.get("FARES_DB_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS))

    log(f"odout version {os.environ.get('VERSION', 'unknown')}")

    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    with duckdb.connect(
        config={"extension_directory": EXTENSION_DIRECTORY, "temp_directory": TEMP_DIRECTORY}
    ) as db:
        # load every extension up front: lock_down blocks loading them from disk later
        db.execute("LOAD quack; LOAD httpfs; LOAD aws; LOAD delta;")
        create_s3_secret(db)
        pin_quack_callbacks(db)
        # after the secret and callbacks; before the first refresh, so a lockdown that
        # blocks the catalog fails at startup
        lock_down(db, data_prefix(springboard))

        catalog = Catalog(db, uri)
        catalog.refresh()

        # quack_serve returns the token: do not print or log its result.
        db.execute(
            "CALL quack_serve(?, allow_other_hostname=true, token=?)", [QUACK_ENDPOINT, token]
        )
        del token
        log("Quack listening on HTTP port 9494; TLS must terminate upstream.")

        while not stopped.wait(refresh_seconds):
            try:
                catalog.refresh()
            except Exception as exception:
                # keep serving the last good attachment; retry on the next interval
                log(f"catalog refresh failed: {exception}")

        log("Stopping")
        db.execute("CALL quack_stop(?)", [QUACK_ENDPOINT])


if __name__ == "__main__":
    main()
