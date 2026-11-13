"""Run raw-expiration and lock/index tests against an isolated local PostgreSQL 17."""

import os
import secrets
import subprocess
import sys
import time
from uuid import uuid4

from sqlalchemy.engine import URL

from tests.integration.verify_db_migrations import docker, scram_verifier


def main() -> None:
    name = "raw-retention-check-" + uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--cpus",
        "0.5",
        "--memory",
        "512m",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={scram_verifier(password)}",
        "-e",
        "POSTGRES_DB=raw_retention_check",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "postgres:17-alpine",
    )
    try:
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "postgres"],
                capture_output=True,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL readiness timed out")
        port = int(docker("port", container, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        url = URL.create(
            "postgresql+asyncpg",
            username="postgres",
            password=password,
            host="127.0.0.1",
            port=port,
            database="raw_retention_check",
        )
        env = dict(os.environ)
        env["TEST_POSTGRES_URL"] = url.render_as_string(hide_password=False)
        subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "tests/test_raw_retention.py", "--postgresql"],
            env=env,
            check=True,
            timeout=600,
        )
    finally:
        docker("rm", "-f", "-v", container)
    print("PostgreSQL raw-retention checks passed; isolated container/volume removed")


if __name__ == "__main__":
    main()
