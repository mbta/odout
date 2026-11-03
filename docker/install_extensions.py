"""
Install the pinned DuckDB core extensions into the image.

Official extension channels can move independently of DuckDB releases. Verify the exact
installed binaries and fail rather than silently upgrading: when a pin fails, review the new
upstream artifact before updating its hash.
"""

import hashlib
import sys
from pathlib import Path

import duckdb

EXTENSION_DIRECTORY = "/opt/duckdb/extensions"
PINS = {
    "quack": "aa0155c452a882eb8912d59589626a5156ab92a8e80952371b78e26e1cf07168",
    "httpfs": "887c392b1e49128d11667c81e3698d8b00dfdeb456771acf66d05a0f74f7b7d8",
    "aws": "1bbf923fb64efd33a6ae69fb96d74cbd474fd003cfd3d5127c06f9a9a6901156",
}

with duckdb.connect(config={"extension_directory": EXTENSION_DIRECTORY}) as db:
    for name, expected in PINS.items():
        db.execute(f"INSTALL {name} FROM core")
        path = Path(EXTENSION_DIRECTORY, f"v{duckdb.__version__}", "linux_amd64")
        digest = hashlib.sha256((path / f"{name}.duckdb_extension").read_bytes()).hexdigest()
        if digest != expected:
            sys.exit(f"{name} artifact changed: review upstream before updating its pin")
        db.execute(f"LOAD {name}")
    print(
        db.execute(
            "SELECT extension_name, extension_version FROM duckdb_extensions() "
            "WHERE extension_name IN ('quack', 'httpfs', 'aws')"
        ).fetchall()
    )
