"""Tests for StateEngine transition, hysteresis, and reset."""

from src.app.services.state_engine import StateEngine


def test_state_engine_transitions() -> None:
    """Test transitions between NORMAL, WARNING, and CRITICAL states."""
    engine = StateEngine()
    pit_id = "test-pit-01"

    # 1. Below warning threshold (e.g. 50%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 50.0, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "NORMAL"
    assert not is_new_alert

    # 2. Reaches warning threshold (76%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 76.0, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "WARNING"
    assert not is_new_alert

    # 3. Reaches critical threshold (86%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 86.0, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "CRITICAL"
    assert is_new_alert is True

    # 4. Remains in critical with slightly higher ratio (87%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 87.0, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "CRITICAL"
    assert is_new_alert is False  # Already triggered

    # 5. Hysteresis: drops slightly to 84.5% (within hysteresis margin of 2.0% from 85.0%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 84.5, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "CRITICAL"  # Holds CRITICAL due to hysteresis buffer!

    # 6. Drops well below critical threshold (e.g. 80.0% < 83.0%)
    state, is_new_alert = engine.evaluate_state(
        pit_id, 80.0, warning_threshold=75.0, critical_threshold=85.0
    )
    assert state == "WARNING"

    # 7. Explicit reset after scrap haul
    engine.reset_pit_state(pit_id, residual_fill_ratio=10.0)
    pit_state = engine.get_or_create_pit_state(pit_id)
    assert pit_state.current_state == "NORMAL"
    assert pit_state.alert_triggered is False
