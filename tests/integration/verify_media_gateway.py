"""Run explicitly with uv; Docker is needed for real Nginx authentication/byte-path checks."""

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "scrap-backend-issue25:local"
CLIP_ID = "550e8400-e29b-41d4-a716-446655440000"
CONTENT = b"synthetic-mp4-content-for-range-verification"
EDGE_TOKEN = "synthetic-v2-gateway-token"


async def probe_edge_v2(client: httpx.AsyncClient) -> None:
    """Exercise v2 auth and a >1MiB scan through real Nginx and the built backend."""
    now = datetime.now(UTC).isoformat()
    observation = {
        "sensor_id": "lidar-a",
        "availability": "PRESENT",
        "calibration_version": None,
        "transform_state": "UNAVAILABLE",
        "transform_reason_codes": ["NO_CALIBRATION"],
        "angle_intervals_mdeg": [{"start_inclusive": 0, "end_exclusive": 180000}],
        "scan": {
            "scan_id": "gateway-scan-1",
            "stream_instance_id": "gateway-stream-1",
            "sequence": 1,
            "acquired_at": now,
            "timestamp_source": "EDGE_SCAN_RECEIVED",
            "clock_domain_id": "gateway-boot-1",
            "acquired_monotonic_ns": "1000000000",
            "clock_state": "SYNCED",
            "clock_offset_ms": 0,
            "source_point_count": 12000,
            "selected_point_count": 12000,
            "points": [
                {
                    "index": i,
                    "angle_mdeg": i * 10,
                    "distance_mm": 1000,
                    "quality": 47,
                    "position_mm": None,
                    "calculation_reason_codes": [],
                }
                for i in range(12000)
            ],
        },
    }
    payload = {
        "schema_version": "2.0",
        "message_type": "lidar_measurement",
        "message_id": "gateway-m-1",
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "generated_at": now,
        "config_revision": "gateway-cfg-1",
        "delivery_kind": "PERIODIC",
        "report_interval_ms": 1000,
        "selection_policy": "LATEST_TIME_MATCHED_PAIR",
        "coordinate_frame_id": "gateway-frame",
        "pair_state": "PARTIAL",
        "pair_delta_ms": None,
        "sensors": [
            observation,
            {**observation, "sensor_id": "lidar-b", "availability": "NO_NEW_SCAN", "scan": None},
        ],
    }
    path = "/api/edge/v2/measurements"
    encoded = json.dumps(payload).encode()
    assert len(encoded) > 1024 * 1024
    headers = {"Idempotency-Key": "gateway-m-1", "Content-Type": "application/json"}
    assert (await client.post(path, content=encoded, headers=headers)).status_code == 401
    headers["Authorization"] = "Bearer " + EDGE_TOKEN
    first = await client.post(path, content=encoded, headers=headers)
    assert first.status_code == 201, first.text
    replay = await client.post(path, content=encoded, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "duplicate": True}


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    )
    if args[0] == "logs":
        return (result.stdout + result.stderr).strip()
    return result.stdout.strip()


async def probe(base_url: str, backend: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
        async with asyncio.timeout(20):
            while True:
                try:
                    if (await client.get("/api/v1/healthz")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.25)
        await probe_edge_v2(client)
        content = f"/media/clips/{CLIP_ID}/content"
        assert (await client.get(content)).status_code == 401
        assert (await client.get("/api/v1/settings/alerts")).status_code == 401
        assert (await client.get("/api/v1/config/pits")).status_code == 401
        session = await client.post(
            "/api/v1/session",
            json={
                "username": "admin",
                "password": "synthetic-gateway-admin-password",
            },
        )
        assert session.status_code == 200
        admin_csrf = {"X-CSRF-Token": session.json()["csrfToken"]}
        settings = await client.get("/api/v1/settings/alerts")
        assert settings.status_code == 200
        assert settings.headers["cache-control"] == "no-store"
        policy_payload = settings.json()
        policy_payload.pop("version")
        policy_payload.update(
            preCollectionAlert={"enabled": False, "thresholdPercent": 70},
            sendDelayMinutes=4,
            repeatIntervalMinutes=45,
            maximumRepeatCount=7,
            recoveryNotificationEnabled=False,
            eventTypes=["device_error"],
        )
        settings_path = "/api/v1/settings/alerts"
        assert (
            await client.put(settings_path, json=policy_payload, headers=admin_csrf)
        ).status_code == 428
        saved = await client.put(
            settings_path,
            json=policy_payload,
            headers={**admin_csrf, "If-Match": settings.headers["etag"]},
        )
        assert saved.status_code == 200, saved.text
        stale = await client.put(
            settings_path,
            json=policy_payload,
            headers={**admin_csrf, "If-Match": settings.headers["etag"]},
        )
        assert stale.status_code == 412
        docker("restart", backend)
        async with asyncio.timeout(20):
            while True:
                try:
                    if (await client.get("/api/v1/healthz")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.25)
        restored = await client.get(settings_path)
        assert restored.status_code == 200 and restored.json() == saved.json()
        assert restored.headers["etag"] == saved.headers["etag"]
        pit_path = "/api/v1/config/pits/gateway-synthetic"
        pit_payload = {"name": "Synthetic gateway pit"}
        assert (await client.put(pit_path, json=pit_payload)).status_code == 403
        created_pit = await client.put(pit_path, json=pit_payload, headers=admin_csrf)
        assert created_pit.status_code == 200, created_pit.text
        assert (await client.get(pit_path)).json()["name"] == "Synthetic gateway pit"
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
        daily_api = "/api/v2/recordings/daily"
        daily = f"/media/daily-recordings/{CLIP_ID}"
        csrf = {"X-CSRF-Token": session.json()["csrfToken"]}
        page = await client.get(daily_api)
        assert page.status_code == 200, page.text
        assert page.json()["items"][0]["playback_path"] == daily + "/hls/index.m3u8"
        assert (await client.post(f"{daily_api}/{CLIP_ID}/exports")).status_code == 403
        prepared = await client.post(f"{daily_api}/{CLIP_ID}/exports", headers=csrf)
        assert prepared.status_code == 202, prepared.text
        detail = await client.get(prepared.headers["location"])
        assert detail.json()["export_state"] == "ready"
        manifest = await client.get(daily + "/hls/index.m3u8")
        assert manifest.status_code == 200, manifest.text
        assert "/api/v2/daily-recordings/" not in manifest.text
        assert "#EXT-X-DISCONTINUITY" in manifest.text
        segment_path = next(
            line for line in manifest.text.splitlines() if line.startswith("/media/")
        )
        segment = await client.get(segment_path)
        assert segment.status_code == 200 and segment.content == CONTENT
        assert segment.headers["content-type"] == "video/mp2t"
        partial = await client.get(segment_path, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == CONTENT[:4]
        assert (await client.head(segment_path)).headers["content-length"] == str(len(CONTENT))
        download = await client.get(detail.json()["download_path"], headers={"Range": "bytes=0-3"})
        assert download.status_code == 206 and download.content == CONTENT[:4]
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["cache-control"] == "no-store"
        assert (await client.head(daily + "/content")).status_code == 200
        assert (await client.post(daily + "/content")).status_code == 405
        assert (await client.get(daily + "/private-file")).status_code == 404
        deleted = await client.delete(f"{daily_api}/{CLIP_ID}", headers=csrf)
        assert deleted.status_code == 204, deleted.text
        assert (await client.get(daily + "/content")).status_code == 410
        logout = await client.delete(
            "/api/v1/session",
            headers={
                "X-CSRF-Token": session.json()["csrfToken"],
            },
        )
        assert logout.status_code == 204
        assert (await client.get(content)).status_code == 401
        assert (await client.get(daily + "/hls/index.m3u8")).status_code == 401
        assert (await client.get(segment_path)).status_code == 401
        assert (await client.get(daily + "/content")).status_code == 401
    print(
        "Gateway checks passed: edge v2 large scan/ACK, session auth, "
        "admin permissions, policy restart, MJPEG, daily HLS/export, Range and logout"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", default=IMAGE, help="Backend image built from the current source"
    )
    args = parser.parse_args()
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
            "ADMIN_PASSWORD=synthetic-gateway-admin-password",
            "-e",
            "DATABASE_URL=sqlite+aiosqlite:////app/data/gateway-test.db",
            "-e",
            "MEDIA_SERVICE_URL=http://media:8889/api/v1",
            "-e",
            "MEDIA_API_TOKEN=synthetic-media-token",
            "-e",
            "EDGE_V2_DEVICES="
            + json.dumps(
                [
                    {
                        "site_id": "site-demo",
                        "edge_id": "edge-demo",
                        "sensor_ids": ["lidar-a", "lidar-b"],
                        "token": EDGE_TOKEN,
                    }
                ]
            ),
            args.image,
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
        asyncio.run(probe(f"http://{address}", backend))
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
