"""Exercise the real deployment Compose with synthetic secrets and isolated local volumes."""

import argparse
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from tests.integration.verify_db_migrations import scram_verifier

ROOT = Path(__file__).resolve().parents[2]


def invoke(args, environment, *, success=True, timeout=120):
    result = subprocess.run(
        args, env=environment, cwd=ROOT, capture_output=True, text=True, timeout=timeout
    )
    # Never echo Compose output/inspect/config: they can contain credentials.
    if success and result.returncode:
        raise RuntimeError("Deployment command failed; synthetic validation did not pass")
    return result


PROBE = r"""
import asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from src.app.db.session import get_engine, close_db
from src.app.db.migration import require_current_schema

async def main():
    engine = get_engine()
    try:
        async with engine.connect() as db:
            await db.run_sync(require_current_schema)
            role = await db.scalar(text("SELECT current_user"))
            assert role in ("scrap_app", "scrap_backup")
            assert not await db.scalar(text(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                "FROM pg_roles WHERE rolname = current_user"
            ))
            assert await db.scalar(text("SELECT count(*) FROM pit_configs")) == 1
            await db.rollback()
            blocked = [
                "CREATE TABLE public.forbidden_probe (id integer)",
                "CREATE ROLE forbidden_probe",
                "UPDATE alembic_version SET version_num = 'forbidden'",
                "SELECT setval('alert_logs_id_seq', 1000)",
            ]
            if role == "scrap_backup":
                blocked.append("UPDATE pit_configs SET name = 'forbidden'")
                blocked.append("SELECT nextval('alert_logs_id_seq')")
            for sql in blocked:
                try:
                    async with db.begin():
                        await db.execute(text(sql))
                except DBAPIError as error:
                    assert error.orig.sqlstate == "42501"
                else:
                    raise AssertionError("Permission boundary failed")
            if role == "scrap_app":
                async with db.begin():
                    await db.execute(text(
                        "UPDATE pit_configs SET name = 'Synthetic runtime write'"
                    ))
                assert await db.scalar(text("SELECT nextval('alert_logs_id_seq')")) > 0
            else:
                assert await db.scalar(text("SELECT last_value FROM alert_logs_id_seq")) > 0
        print("Role permissions passed")
    finally:
        await close_db()

asyncio.run(main())
"""

SEED = r"""
import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.app.db.session import get_engine, close_db
from src.app.models.config import PitConfig

async def main():
    try:
        factory = async_sessionmaker(get_engine())
        async with factory() as db:
            db.add(PitConfig(id="pit-deploy-check", name="Synthetic deployment pit"))
            await db.commit()
    finally:
        await close_db()

asyncio.run(main())
"""


def verify(image, directory):
    project = "pg-deploy-check-" + uuid4().hex[:10]
    settings = directory / "backend.env"
    # This file contains only public synthetic settings, never generated passwords.
    settings.write_text(
        "EDGE_API_KEY=synthetic-deployment-edge-key\nCORS_ORIGINS=https://dashboard.example.invalid\n"
    )
    credentials = {role: secrets.token_urlsafe(32) for role in ("ADMIN", "OWNER", "APP", "BACKUP")}
    login_password = secrets.token_urlsafe(32)
    environment = dict(
        os.environ,
        COMPOSE_PROJECT_NAME=project,
        BACKEND_IMAGE=image,
        POSTGRES_IMAGE="postgres:17-alpine",
        BACKEND_ENV_FILE=str(settings),
        BACKEND_PORT="0",
        ADMIN_PASSWORD=login_password,
        DB_OWNER_PASSWORD_URLENCODED=quote(credentials["OWNER"], safe=""),
        DB_APP_PASSWORD_URLENCODED=quote(credentials["APP"], safe=""),
        **{f"DB_{role}_SCRAM": scram_verifier(password) for role, password in credentials.items()},
    )
    # Disable implicit .env loading; only the selected public settings file and memory are used.
    base = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        project,
        "-f",
        "docker-compose.postgresql.yml",
    ]

    def compose(*args, success=True, env=None):
        return invoke([*base, *args], env or environment, success=success)

    def inspect(service):
        container = compose("ps", "-q", service).stdout.strip()
        assert container, service
        return json.loads(invoke(["docker", "inspect", container], environment).stdout)[0]

    def request(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == 200, f"HTTP check failed for {path}"
        return response

    try:
        compose("config", "--quiet")
        missing = {k: v for k, v in environment.items() if k != "DB_APP_PASSWORD_URLENCODED"}
        assert compose("config", "--quiet", env=missing, success=False).returncode != 0
        compose("up", "-d", "--wait", "--wait-timeout", "90", "postgres")
        db = inspect("postgres")
        assert not any(db["NetworkSettings"]["Ports"].values()), "PostgreSQL port was published"
        network_name = next(iter(db["NetworkSettings"]["Networks"]))
        network = json.loads(
            invoke(["docker", "network", "inspect", network_name], environment).stdout
        )[0]
        assert network["Internal"] is True
        assert all(
            password not in "\n".join(db["Config"]["Env"]) for password in credentials.values()
        )
        roles = compose(
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--dbname=scrap_monitoring",
            "--tuples-only",
            "--no-align",
            "--command=SELECT rolname, rolpassword FROM pg_authid WHERE rolname LIKE 'scrap_%'",
        ).stdout
        for role in ("OWNER", "APP", "BACKUP"):
            assert environment[f"DB_{role}_SCRAM"] in roles, "SCRAM storage mismatch"
        assert all(password not in roles for password in credentials.values())
        print(
            "Internal network, unpublished DB port and verifier-only initialization passed",
            flush=True,
        )

        # Production must not silently create its schema when the app starts.
        before_migration = compose("run", "--rm", "--no-deps", "backend", success=False)
        assert before_migration.returncode != 0 and "migration required" in before_migration.stderr
        compose("run", "--rm", "manage", "-m", "src.app.cli.db", "upgrade")
        compose(
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "psql",
            "--no-password",
            "--dbname=scrap_monitoring",
            "--file=/opt/scrap-postgresql/runtime-grants.sql",
        )
        compose("run", "--rm", "manage", "-m", "src.app.cli.db", "check")
        compose("run", "--rm", "manage", "-c", SEED)
        compose("run", "--rm", "--entrypoint", "python", "backend", "-c", PROBE)

        # Override only this one-off container's URL in memory to test the read-only role.
        backup_url = (
            "postgresql+asyncpg://scrap_backup:"
            + quote(credentials["BACKUP"], safe="")
            + "@postgres:5432/scrap_monitoring"
        )
        compose(
            "run",
            "--rm",
            "-e",
            "DATABASE_URL",
            "manage",
            "-c",
            PROBE,
            env=dict(environment, DATABASE_URL=backup_url),
        )
        # The backup role must also work with the real pg_dump client over SCRAM/TCP.
        # Docker reads PGPASSWORD from this process environment, never a command argument/file.
        backup = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network_name,
                "-e",
                "PGPASSWORD",
                "postgres:17-alpine",
                "pg_dump",
                "--no-password",
                "--host=postgres",
                "--username=scrap_backup",
                "--dbname=scrap_monitoring",
                "--format=custom",
            ],
            env=dict(environment, PGPASSWORD=credentials["BACKUP"]),
            cwd=ROOT,
            capture_output=True,
            timeout=120,
        )
        assert backup.returncode == 0 and backup.stdout.startswith(b"PGDMP")
        wrong = dict(environment, DB_OWNER_PASSWORD_URLENCODED=secrets.token_urlsafe(32))
        rejected = compose(
            "run", "--rm", "manage", "-m", "src.app.cli.db", "check", env=wrong, success=False
        )
        assert rejected.returncode != 0, "Incorrect migration password was accepted"
        assert all(
            password not in rejected.stdout + rejected.stderr for password in credentials.values()
        )
        print(
            "Explicit migration, runtime/backup permissions and wrong-password rejection passed",
            flush=True,
        )

        compose("up", "-d", "--wait", "--wait-timeout", "90", "backend")
        port = compose("port", "backend", "8000").stdout.strip().rsplit(":", 1)[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            request(client, "GET", "/api/v1/readyz")
            login = request(
                client,
                "POST",
                "/api/v1/session",
                json={"username": "admin", "password": login_password},
            )
            assert "Secure" in login.headers["set-cookie"]
            cookie = login.cookies["scrap_session"]
            # Production cookies require HTTPS. This explicit header is only for the local probe.
            headers = {"Cookie": f"scrap_session={cookie}"}
            request(client, "GET", "/api/v1/session", headers=headers)
            assert request(client, "GET", "/api/v1/config/pits", headers=headers).json()

        # A new initialization verifier must not reset accounts on an existing volume.
        changed = dict(
            environment,
            DB_APP_SCRAM=scram_verifier(secrets.token_urlsafe(32)),
            ADMIN_PASSWORD=secrets.token_urlsafe(32),
        )
        compose("down", "--remove-orphans")  # Deliberately keep the named DB volume.
        compose("up", "-d", "--wait", "--wait-timeout", "90", "postgres", env=changed)
        compose("up", "-d", "--wait", "--wait-timeout", "90", "backend", env=changed)
        port = compose("port", "backend", "8000").stdout.strip().rsplit(":", 1)[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            request(client, "GET", "/api/v1/readyz")
            request(client, "GET", "/api/v1/session", headers=headers)
            request(
                client,
                "POST",
                "/api/v1/session",
                json={"username": "admin", "password": login_password},
            )
            pits = request(client, "GET", "/api/v1/config/pits", headers=headers).json()
            assert "pit-deploy-check" in json.dumps(pits)
            assert "Synthetic runtime write" in json.dumps(pits)
        compose("run", "--rm", "manage", "-m", "src.app.cli.db", "check")
        print(
            "Backend, persistent schema/data/session and existing credentials passed",
            flush=True,
        )
    finally:
        # Only this randomly named test project is removed, including its synthetic volume.
        compose("down", "--volumes", "--remove-orphans")

    try:
        # Matches the outer format, but has invalid SCRAM key lengths. PostgreSQL
        # would otherwise hash this value as a plaintext password and accept it.
        invalid = dict(environment, DB_APP_SCRAM="SCRAM-SHA-256$4096:YQ==$YQ==:YQ==")
        compose("up", "-d", "postgres", env=invalid)
        for _ in range(45):
            db = inspect("postgres")
            ready = compose(
                "exec", "-T", "postgres", "pg_isready", "-h", "127.0.0.1", success=False
            )
            if db["RestartCount"] and ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Failed-initialization restart was not observed")
        assert db["State"]["Health"]["Status"] != "healthy"
        marker = compose(
            "exec",
            "-T",
            "postgres",
            "test",
            "-f",
            "/var/lib/postgresql/data/pgdata/.scrap-initialized",
            success=False,
        )
        assert marker.returncode != 0
        print("Invalid verifier initialization cannot become healthy after restart", flush=True)
    finally:
        compose("down", "--volumes", "--remove-orphans")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="Backend image built from the current source"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="deployment-check-", dir=ROOT.parent) as directory:
        verify(args.image, Path(directory))
    print("PostgreSQL deployment checks passed; synthetic containers/volumes removed")


if __name__ == "__main__":
    main()
