"""Run API/storage tests against disposable, migrated PostgreSQL schemas."""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.verify_db_migrations import docker, scram_verifier

ROOT = Path(__file__).resolve().parents[2]


async def wait_ready(url: URL) -> None:
    engine = create_async_engine(url)
    try:
        for _ in range(60):
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                return
            except Exception:
                await asyncio.sleep(0.5)
        raise RuntimeError("PostgreSQL readiness timed out")
    finally:
        await engine.dispose()


def main() -> int:
    name = "pg-api-check-" + uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--cpus",
        "1",
        "--memory",
        "512m",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={scram_verifier(password)}",
        "-e",
        "POSTGRES_DB=api_check",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "postgres:17-alpine",
    )
    try:
        port = int(docker("port", container, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        url = URL.create(
            "postgresql+asyncpg",
            username="postgres",
            password=password,
            host="127.0.0.1",
            port=port,
            database="api_check",
        )
        asyncio.run(wait_ready(url))
        # This child-process environment stays in memory; no credential file or
        # plaintext Docker environment/command argument is created.
        environment = dict(os.environ, TEST_POSTGRES_URL=url.render_as_string(hide_password=False))
        return subprocess.run(
            [sys.executable, "-m", "pytest", "--postgresql", "-v", *sys.argv[1:]],
            cwd=ROOT,
            env=environment,
            timeout=600,
            check=False,
        ).returncode
    finally:
        docker("rm", "-f", "-v", container)
        print("Temporary PostgreSQL container and volume removed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
