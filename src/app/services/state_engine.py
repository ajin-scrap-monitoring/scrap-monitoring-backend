"""State Engine for scrap level classification, anti-chattering, and hysteresis."""

import logging
from dataclasses import dataclass

from src.app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class PitCurrentState:
    """Internal state tracker for a pit."""

    pit_id: str
    current_state: str = "NORMAL"
    last_fill_ratio: float = 0.0
    alert_triggered: bool = False


class StateEngine:
    """Classifies scrap loading states and handles hysteresis to avoid chattering."""

    def __init__(self) -> None:
        self._pit_states: dict[str, PitCurrentState] = {}

    def get_or_create_pit_state(self, pit_id: str) -> PitCurrentState:
        """Get or initialize state tracking for a pit."""
        if pit_id not in self._pit_states:
            self._pit_states[pit_id] = PitCurrentState(pit_id=pit_id)
        return self._pit_states[pit_id]

    def reset_pit_state(self, pit_id: str, residual_fill_ratio: float = 0.0) -> None:
        """Explicitly reset pit state after scrap collection/haul."""
        pit_state = self.get_or_create_pit_state(pit_id)
        pit_state.current_state = "NORMAL"
        pit_state.last_fill_ratio = residual_fill_ratio
        pit_state.alert_triggered = False
        logger.info("Pit %s state explicitly reset to NORMAL after collection.", pit_id)

    def evaluate_state(
        self,
        pit_id: str,
        fill_ratio_percent: float,
        warning_threshold: float | None = None,
        critical_threshold: float | None = None,
    ) -> tuple[str, bool]:
        """Evaluate scrap level state with hysteresis.

        Returns:
            tuple[state, is_new_critical_alert_candidate]
        """
        settings = get_settings()
        warn_th = (
            warning_threshold
            if warning_threshold is not None
            else settings.DEFAULT_WARNING_THRESHOLD
        )
        crit_th = (
            critical_threshold
            if critical_threshold is not None
            else settings.DEFAULT_CRITICAL_THRESHOLD
        )

        # Hysteresis margin (2.0% buffer below threshold before resetting down)
        hysteresis_margin = 2.0

        pit_state = self.get_or_create_pit_state(pit_id)
        prev_state = pit_state.current_state
        new_state = prev_state
        is_new_critical = False

        if fill_ratio_percent >= crit_th:
            new_state = "CRITICAL"
            if not pit_state.alert_triggered:
                is_new_critical = True
                pit_state.alert_triggered = True
        elif fill_ratio_percent >= warn_th:
            # If previously CRITICAL, downgrade only if below (crit_th - margin)
            if prev_state == "CRITICAL":
                if fill_ratio_percent < (crit_th - hysteresis_margin):
                    new_state = "WARNING"
                else:
                    new_state = "CRITICAL"
            else:
                new_state = "WARNING"
        else:
            # Below warning threshold
            if prev_state == "CRITICAL":
                if fill_ratio_percent < (warn_th - hysteresis_margin):
                    new_state = "NORMAL"
                    pit_state.alert_triggered = False
                else:
                    new_state = "WARNING"
            elif prev_state == "WARNING":
                if fill_ratio_percent < (warn_th - hysteresis_margin):
                    new_state = "NORMAL"
                else:
                    new_state = "WARNING"
            else:
                new_state = "NORMAL"

        pit_state.current_state = new_state
        pit_state.last_fill_ratio = fill_ratio_percent

        return new_state, is_new_critical


# Global State Engine Singleton
state_engine = StateEngine()
