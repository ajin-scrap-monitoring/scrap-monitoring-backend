"""Validate saved thresholds against the existing operational recovery rule."""


def validate_alert_thresholds(warning: float, critical: float) -> None:
    """Keep the warning threshold at or below the five-point recovery boundary."""
    if not 0 <= warning <= critical - 5.0 < critical <= 100:
        raise ValueError(
            "Warning threshold must be at least 5 percentage points below collection threshold"
        )
