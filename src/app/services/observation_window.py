"""Bounded one-second slots aligned to observations rather than UTC clock boundaries."""

from collections.abc import Sequence
from datetime import datetime, timedelta


def observation_slots(
    times: Sequence[datetime], now: datetime, size: int, anchor: datetime | None
) -> tuple[list[int], datetime]:
    """Select the latest observation nearest each nominal one-second slot.

    The half-second offset rounds to a slot center; it is not an allowance for stale
    input or gaps. Callers still reject repeated/old observations and reset on gaps.
    Renew the anchor within the existing window so accumulated clock drift cannot
    move a long-running stream across a permanent UTC-aligned boundary. Persisting
    the anchor also stops a burst from moving the first slot on every replacement.
    """
    duration = timedelta(seconds=size)
    current = [(index, at) for index, at in enumerate(times) if timedelta(0) <= now - at < duration]
    if not current:
        return [], now
    if anchor is None or not timedelta(0) <= now - anchor < duration:
        anchor = current[0][1]
    slots: dict[int, int] = {}
    for index, at in current:
        slot = (at - anchor + timedelta(milliseconds=500)) // timedelta(seconds=1)
        slots[slot] = index
    return list(slots.values())[-size:], anchor
