"""Run explicitly with uv; Docker is needed for real Nginx authentication/byte-path checks."""

import asyncio
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "scrap-backend-issue25:local"
CLIP_ID = "550e8400-e29b-41d4-a716-446655440000"
CONTENT = b"synthetic-mp4-content-for-range-verification"


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    )
    if args[0] == "logs":
        return (result.stdout + result.stderr).strip()
    return result.stdout.strip()


async def probe(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
        async with asyncio.timeout(20):
            while True:
                try:
                    if (await client.get("/api/v1/healthz")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.25)
        content = f"/media/clips/{CLIP_ID}/content"
        assert (await client.get(content)).status_code == 401
        session = await client.post(
            "/api/v1/session",
            json={
                "username": "admin",
                "password": "admin1234",
            },
        )
        assert session.status_code == 200
        policy = await client.get("/api/v1/media/cameras/camera-main/recording-policy")
        assert policy.status_code == 200 and policy.json()["retention_days"] == 30
        video = await client.get(content)
        assert video.status_code == 200 and video.content == CONTENT
        assert video.headers["content-type"] == "video/mp4"
        assert int(video.headers["content-length"]) == len(CONTENT)
        assert video.headers["accept-ranges"] == "bytes"
        partial = await client.get(content, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == CONTENT[:4]
        assert partial.headers["content-range"] == f"bytes 0-3/{len(CONTENT)}"
        assert partial.headers["content-length"] == "4"
        invalid = await client.get(content, headers={"Range": "bytes=999-"})
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == f"bytes */{len(CONTENT)}"
        multiple = await client.get(content, headers={"Range": "bytes=0-1,3-4"})
        assert multiple.status_code == 200 and multiple.content == CONTENT
        download = await client.get(content, params={"download": "true"})
        assert download.headers["content-disposition"].startswith("attachment;")
        for state, status in (("deleted", 410), ("expired", 410), ("processing", 409)):
            assert (await client.get(content, params={"state": state})).status_code == status
        live = await client.get("/media/cameras/camera-main/live")
        assert live.status_code == 200 and live.content.startswith(b"--frame\r\n")
        assert live.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert live.headers["x-accel-buffering"] == "no"
        unavailable = await client.get("/media/cameras/camera-main/live?unavailable=true")
        assert unavailable.status_code == 503
        assert (await client.post(content)).status_code == 405
        assert (await client.get("/media/clips")).status_code == 404
        assert "synthetic-media-token" not in str(video.headers)
        logout = await client.delete(
            "/api/v1/session",
            headers={
                "X-CSRF-Token": session.json()["csrfToken"],
            },
        )
        assert logout.status_code == 204
        assert (await client.get(content)).status_code == 401
    print("Gateway checks passed: session auth, MJPEG, Range, download, errors and logout")


def main() -> None:
    prefix = f"media-probe-{uuid4().hex[:8]}"
    network = f"{prefix}-network"
    containers: list[str] = []
    docker("network", "create", network)
    try:
        backend = docker(
            "run",
            "-d",
            "--name",
            f"{prefix}-backend",
            "--network",
            network,
            "--network-alias",
            "backend",
            "-e",
            "APP_ENV=test",
            "-e",
            "DATABASE_URL=sqlite+aiosqlite:////app/data/gateway-test.db",
            "-e",
            "MEDIA_SERVICE_URL=http://media:8889/api/v1",
            "-e",
            "MEDIA_API_TOKEN=synthetic-media-token",
            IMAGE,
        )
        containers.append(backend)
        media = docker(
            "run",
            "-d",
            "--name",
            f"{prefix}-media",
            "--network",
            network,
            "--network-alias",
            "media",
            "--user",
            "1000:1000",
            "-v",
            f"{ROOT / 'tests/integration/fake_media_server.py'}:/probe.py:ro",
            "python:3.13-slim",
            "python",
            "/probe.py",
        )
        containers.append(media)
        gateway = docker(
            "run",
            "-d",
            "--name",
            f"{prefix}-gateway",
            "--network",
            network,
            "--user",
            "101:101",
            "--read-only",
            "--tmpfs",
            "/tmp:uid=101,gid=101,mode=0700",
            "-p",
            "127.0.0.1::8080",
            "-e",
            "MEDIA_PROXY_UPSTREAM=media:8889",
            "-e",
            "MEDIA_API_TOKEN=synthetic-media-token",
            "-e",
            "NGINX_ENVSUBST_FILTER=^(MEDIA_PROXY_UPSTREAM|MEDIA_API_TOKEN)$",
            "-e",
            "NGINX_ENVSUBST_OUTPUT_DIR=/tmp",
            "-v",
            f"{ROOT / 'deploy/media-gateway-nginx.conf'}:/etc/nginx/nginx.conf:ro",
            "-v",
            f"{ROOT / 'deploy/media-gateway.conf.template'}:"
            "/etc/nginx/templates/default.conf.template:ro",
            "nginx:1.28-alpine",
        )
        containers.append(gateway)
        address = docker("port", gateway, "8080/tcp").splitlines()[0]
        asyncio.run(probe(f"http://{address}"))
        docker("exec", gateway, "nginx", "-t")
    except Exception:
        for container in containers:
            print(docker("logs", "--tail", "20", container))
        raise
    finally:
        for container in reversed(containers):
            docker("rm", "-f", container)
        docker("network", "rm", network)


if __name__ == "__main__":
    main()
