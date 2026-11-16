"""Keep collection on its existing synthetic cm observations while the display selects sections."""

import pytest

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.collection_detection import CollectionTracker
from src.app.services.collection_detection_service import (
    TRACKER_KIND,
    collection_input,
    process_collection_source,
)
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import monitoring_source
from tests import test_section_profile_service as section
from tests.test_collection_detection import event_rows
from tests.test_edge_world_height import world_inputs
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_section_profile_service import section_context as section_context


@pytest.fixture
def collector(section_context, monkeypatch):
    for key, value in {
        "DETECTION_ENABLED": "true",
        "DROP_METERS": "0.2",
        "STABLE_BAND_METERS": "0.02",
        "BASELINE_SECONDS": "3",
        "CONFIRM_SECONDS": "3",
        "SIMULATION_SETTLE_SECONDS": "5",
    }.items():
        monkeypatch.setenv("EDGE_V2_COLLECTION_" + key, value)
    raw = {"height": 80}
    monkeypatch.setattr(section, "world_inputs", lambda: world_inputs(raw["height"]))
    get_settings.cache_clear()
    yield section_context, raw
    get_settings.cache_clear()


async def step(client, db, clock, worker, second, percent, limited=False):
    snapshot, page = await section.frame(client, db, clock, second, percent, limited=limited)
    await worker.run_once(db, clock["now"])
    return snapshot, page


@pytest.mark.parametrize("basis", ["UPPER", "REPRESENTATIVE"])
async def test_existing_height_drop_detection_survives_constant_section_display(
    client, test_db_session, collector, monkeypatch, basis
):
    clock, raw = collector
    monkeypatch.setenv("EDGE_V2_COLLECTION_HEIGHT_BASIS", basis)
    get_settings.cache_clear()
    db = test_db_session
    await section.prepare(client, db)
    worker = EdgeV2OperationsWorker()
    for second in range(32):
        raw["height"] = 80 if second < 10 else 20
        snapshot, page = await step(client, db, clock, worker, second, 60)
        if second >= 4:
            assert snapshot.summary.loadPercent == page.meanSectionFillPercent == 60
    rows = await event_rows(db)
    assert [r.kind for r in rows] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert rows[0].payload["detectionId"] == rows[1].payload["detectionId"]
    assert all(r.payload["heightBasis"] == basis for r in rows)
    assert all(r.payload["confirmedCompletion"] is False for r in rows)
    view, _, _, height = await collection_input(db, monitoring_source("pit-demo"), clock["now"])
    assert view.summary.inputSource == "HEIGHT_SECTIONS"
    assert view.fillObservation.message_id.startswith("section-measurement-")
    assert height == 20
    assert get_settings().EDGE_V2_COLLECTION_DROP_METERS == 0.2
    from datetime import timedelta

    from tests.test_height_fill_pipeline import START

    history = await load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(minutes=1),
        now=START + timedelta(minutes=1),
    )
    assert [s.valuePercent for s in history.samples if s.valuePercent is not None] == [60]


async def test_area_percentage_drop_is_not_treated_as_a_height_drop(
    client, test_db_session, collector
):
    clock, raw = collector
    db = test_db_session
    await section.prepare(client, db)
    worker = EdgeV2OperationsWorker()
    for second in range(30):
        snapshot, _ = await step(client, db, clock, worker, second, 90 if second < 10 else 10)
    assert raw["height"] == 80
    assert snapshot.summary.loadPercent == 10
    assert await event_rows(db) == []


async def test_limited_display_does_not_stop_the_independent_valid_height_collector(
    client, test_db_session, collector
):
    clock, raw = collector
    db = test_db_session
    await section.prepare(client, db, limited=True)
    worker = EdgeV2OperationsWorker()
    for second in range(32):
        raw["height"] = 80 if second < 10 else 20
        snapshot, page = await step(client, db, clock, worker, second, 95, limited=True)
        assert snapshot.summary.loadPercent is page.meanSectionFillPercent is None
        assert snapshot.dataState == "UNAVAILABLE"
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]


async def test_display_switch_preserves_prepared_collection_scope_and_baseline(
    client, test_db_session, collector, monkeypatch
):
    clock, _ = collector
    monkeypatch.setenv("EDGE_V2_SECTION_PROFILES_ENABLED", "false")
    get_settings.cache_clear()
    db = test_db_session
    await section.prepare(client, db)
    worker = EdgeV2OperationsWorker()
    for second in range(9):
        await step(client, db, clock, worker, second, 60)
    row = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", TRACKER_KIND))
    before = CollectionTracker.model_validate(row.payload)
    assert before.phase == "IDLE" and before.baseline_cm == 80
    monkeypatch.setenv("EDGE_V2_SECTION_PROFILES_ENABLED", "true")
    get_settings.cache_clear()
    await step(client, db, clock, worker, 9, 60)
    after = CollectionTracker.model_validate(row.payload)
    assert after.scope_token == before.scope_token
    assert after.phase == "IDLE" and after.baseline_cm == 80
    assert after.observed_at > before.observed_at


async def test_stale_height_does_not_finish_a_collection_from_elapsed_time(
    client, test_db_session, collector
):
    from datetime import timedelta

    clock, raw = collector
    db = test_db_session
    await section.prepare(client, db)
    worker = EdgeV2OperationsWorker()
    for second in range(16):
        raw["height"] = 80 if second < 10 else 20
        await step(client, db, clock, worker, second, 60)
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED"]
    when = clock["now"] + timedelta(seconds=30)
    source = monitoring_source("pit-demo")
    view, _, _, height = await collection_input(db, source, when)
    assert view.dataState == "STALE" and height is None
    await process_collection_source(db, source, when)
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    state = CollectionTracker.model_validate(row.payload)
    assert not state.input_valid and state.phase == "COLLECTING"
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED"]
