"""Start the persistent loopback PostgreSQL service used by local Thesis.

Credentials are created in the ignored local environment file and passed to
Docker through its process environment, never printed or placed in arguments.
Existing volumes are retained across container and application restarts.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import unquote, urlparse

import psycopg
from dotenv import dotenv_values, set_key

CONTAINER = "thesis-postgres"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    values = dotenv_values(args.env_file)
    dsn = values.get("THESIS_POSTGRES_DSN")
    if not dsn:
        dsn = f"postgresql://thesis:{secrets.token_hex(24)}@127.0.0.1:5433/thesis"
        set_key(args.env_file, "THESIS_POSTGRES_DSN", dsn, quote_mode="never")
    parsed = urlparse(dsn)
    if (
        parsed.hostname != "127.0.0.1"
        or parsed.port != 5433
        or parsed.path != "/thesis"
        or parsed.username != "thesis"
        or not parsed.password
    ):
        raise ValueError(
            "local PostgreSQL launcher requires thesis@127.0.0.1:5433/thesis and a password"
        )
    names = subprocess.check_output(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], text=True
    ).splitlines()
    if CONTAINER in names:
        subprocess.run(["docker", "start", CONTAINER], check=True, capture_output=True)
    else:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                CONTAINER,
                "--restart",
                "unless-stopped",
                "-p",
                "127.0.0.1:5433:5432",
                "-e",
                "POSTGRES_USER=thesis",
                "-e",
                "POSTGRES_DB=thesis",
                "-e",
                "POSTGRES_PASSWORD",
                "-v",
                "thesis-postgres-data:/var/lib/postgresql/data",
                "postgres:17",
            ],
            env={**os.environ, "POSTGRES_PASSWORD": unquote(parsed.password)},
            check=True,
            capture_output=True,
        )
    deadline = monotonic() + 30
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
            break
        except psycopg.OperationalError:
            if monotonic() >= deadline:
                raise RuntimeError("local PostgreSQL did not become ready") from None
            sleep(0.25)
    set_key(args.env_file, "THESIS_PERSISTENCE", "postgres", quote_mode="never")
    print("PostgreSQL ready at 127.0.0.1:5433; persistent volume retained.")


if __name__ == "__main__":
    main()
