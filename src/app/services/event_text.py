"""Display recorded results without fixed input-source labels; never rewrite evidence."""


def event_detail(message: str) -> str:
    """Remove only the exact labels used by the previous alert generator.

    Keep business values, units and arbitrary text intact. Historical storage stays
    unchanged; simulation provenance remains in structured creation evidence.
    """
    prefix = "[시뮬레이션] "
    suffix = " (라이다 v2 판정)"
    if message.startswith(prefix):
        message = message[len(prefix) :]
    if message.endswith(suffix):
        message = message[: -len(suffix)]
    return message
