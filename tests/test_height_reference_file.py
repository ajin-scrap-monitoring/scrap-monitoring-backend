"""Synthetic reference replacement, unsafe-file rejection and uninterrupted API coverage."""

import asyncio
import copy
import json
import os
from datetime import timedelta

import pytest

from src.app.core.config import get_settings
from src.app.core.height_reference_file import ReferenceFileError, read_reference_file
from src.app.services.height_reference import consistent_height_references, height_reference
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from tests.test_height_fill_pipeline import START, frame, prepare, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_lidar_height import REFERENCE, inputs
from tests.test_monitoring_v2 import assessment, login, status
from tests.test_simulation_height import REFERENCE as SIMULATION_REFERENCE


def document(revision="cfg-1"):
    reference = copy.deepcopy(REFERENCE)
    reference["config_revision"] = revision
    return {
        "schema_version": 1,
        "height_references": [reference],
        "simulation_height_references": [],
    }


def replace(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


@pytest.fixture
def reference_file(tmp_path, monkeypatch, height_context):
    path = tmp_path / "height-reference.json"
    replace(path, document())
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCE_FILE", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_atomic_reference_reload_and_failure_never_use_environment_or_stale_cache(reference_file):
    settings = get_settings()
    assert height_reference("pit-demo").config_revision == "cfg-1"
    replace(reference_file, document("cfg-2"))
    assert get_settings() is settings
    assert height_reference("pit-demo").config_revision == "cfg-2"
    reference_file.write_text("invalid")
    assert height_reference("pit-demo") is None
    reference_file.unlink()
    assert height_reference("pit-demo") is None
    replace(reference_file, document("cfg-3"))
    assert height_reference("pit-demo").config_revision == "cfg-3"


@pytest.mark.parametrize("kind", ["public", "symlink", "fifo", "oversize", "directory"])
def test_unsafe_private_file_is_bounded_and_rejected(reference_file, kind):
    if kind == "public":
        reference_file.chmod(0o644)
    elif kind == "oversize":
        reference_file.write_bytes(b"x" * 131073)
    else:
        reference_file.unlink()
        if kind == "symlink":
            target = reference_file.with_suffix(".target")
            replace(target, document())
            reference_file.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(reference_file, 0o600)
        else:
            reference_file.mkdir(mode=0o700)
    assert height_reference("pit-demo") is None
    with pytest.raises(ReferenceFileError, match="unavailable or invalid"):
        read_reference_file(reference_file, get_settings())


@pytest.mark.parametrize(
    "kind", ["schema", "extra", "duplicate", "pit", "sensors", "geometry", "simulation_production"]
)
def test_file_cannot_bypass_existing_reference_guards(reference_file, kind, monkeypatch):
    value = document()
    reference = value["height_references"][0]
    if kind == "schema":
        value["schema_version"] = 2
    elif kind == "extra":
        value["EDGE_V2_ASSESSMENT_TTL_SECONDS"] = 999
    elif kind == "duplicate":
        value["height_references"].append(copy.deepcopy(reference))
    elif kind == "pit":
        reference["pit_id"] = "different-pit"
    elif kind == "sensors":
        reference["calibration_versions"] = {"different-a": "v1", "different-b": "v1"}
    elif kind == "geometry":
        reference["x_max_cm"] = reference["x_min_cm"]
    else:
        monkeypatch.setenv("APP_ENV", "production")
        get_settings.cache_clear()
        value["height_references"] = []
        value["simulation_height_references"] = [SIMULATION_REFERENCE]
    replace(reference_file, value)
    assert height_reference("pit-demo") is None


async def test_operation_pins_across_awaits_while_next_task_uses_replacement(reference_file):
    entered, proceed = asyncio.Event(), asyncio.Event()

    @consistent_height_references
    async def inner():
        return height_reference("pit-demo").config_revision

    @consistent_height_references
    async def operation():
        first = await inner()
        entered.set()
        await proceed.wait()
        return first, await inner()

    task = asyncio.create_task(operation())
    await entered.wait()
    replace(reference_file, document("cfg-2"))
    assert await inner() == "cfg-2"
    proceed.set()
    assert await task == ("cfg-1", "cfg-1")
    assert await inner() == "cfg-2"


async def test_reference_scope_resets_after_failure(reference_file):
    @consistent_height_references
    async def failing():
        replace(reference_file, document("cfg-2"))
        assert height_reference("pit-demo").config_revision == "cfg-1"
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        await failing()
    assert height_reference("pit-demo").config_revision == "cfg-2"


async def test_cancelled_operation_releases_its_reference_scope(reference_file):
    entered = asyncio.Event()

    @consistent_height_references
    async def operation():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replace(reference_file, document("cfg-2"))
    assert height_reference("pit-demo").config_revision == "cfg-2"


def test_cli_checks_without_writing_or_disclosing_geometry(reference_file, monkeypatch, capsys):
    from src.app.cli.height_references import main

    original = reference_file.read_bytes()
    monkeypatch.setattr("sys.argv", ["height_references", "--file", str(reference_file)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "valid": True,
        "heightReferences": 1,
        "simulationHeightReferences": 0,
    }
    assert reference_file.read_bytes() == original
    reference_file.write_text("invalid")
    assert main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "valid": False,
        "reason": "HEIGHT_REFERENCE_FILE_INVALID",
    }


async def test_new_configuration_ack_stays_blocked_until_explicit_file_update(
    client, test_db_session, reference_file, height_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(6):
        _, snapshot = await frame(client, db, height_context, second, 50)
    assert snapshot.summary.filterState == "READY"
    c, raw = inputs()
    c.update(config_revision="cfg-2", message_id="configuration-revision-2")
    c["generated_at"] = (START + timedelta(seconds=6)).isoformat()
    assert (await send(client, c)).status_code == 201
    source = monitoring_source("pit-demo")
    settings = get_settings()
    for second in range(6, 13):
        now = START + timedelta(seconds=second)
        height_context["now"] = now
        s = status(f"revision2-status-{second}", now, second + 2)
        s["config_revision"] = "cfg-2"
        assert (await send(client, s)).status_code == 201
        m = copy.deepcopy(raw)
        m.update(
            config_revision="cfg-2",
            message_id=f"revision2-measurement-{second}",
            generated_at=now.isoformat(),
        )
        for index, sensor in enumerate(m["sensors"]):
            sensor["scan"].update(
                scan_id=f"revision2-scan-{index}-{second}",
                sequence=second,
                acquired_at=now.isoformat(),
                acquired_monotonic_ns=str((second + 1) * 1_000_000_000),
            )
        assert (await send(client, m)).status_code == 201
        a = assessment(f"revision2-state-{second}", 0.5, now)
        a["config_revision"] = "cfg-2"
        assert (await send(client, a)).status_code == 201
        page = await lidar_profiles(db, source, now)
        snapshot = await build_snapshot(db, source, now)
        if second == 6:
            assert page.meanSectionFillPercent is None
            assert snapshot.summary.loadPercent is None
            replace(reference_file, document("cfg-2"))
        elif second == 7:
            assert snapshot.summary.filterState == "WARMING"
    assert get_settings() is settings
    assert snapshot.summary.filterState == "READY"
    assert snapshot.summary.loadPercent is not None
    # Corrupting a previously accepted file never leaves its last geometry active.
    reference_file.write_text("invalid")
    snapshot = await build_snapshot(db, source, now)
    assert snapshot.summary.loadPercent is None
    assert (await lidar_profiles(db, source, now)).meanSectionFillPercent is None
    await login(client)
    snapshot_response = await client.get(
        "/api/v2/monitoring/snapshot", params={"pit_id": "pit-demo"}
    )
    profiles_response = await client.get(
        "/api/v2/monitoring/profiles", params={"pit_id": "pit-demo"}
    )
    assert snapshot_response.status_code == profiles_response.status_code == 200
    assert snapshot_response.json()["summary"]["loadPercent"] is None
