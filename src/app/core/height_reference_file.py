"""Bounded private reference loading; invalid replacements never select stale geometry."""

import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.app.core.config import Settings
from src.app.schemas.lidar_height import HeightReference, SimulationHeightReference

MAX_REFERENCE_FILE_BYTES = 131072


class ReferenceFileError(ValueError):
    """Deliberately excludes private paths, geometry and validation input."""


class HeightReferenceDocument(BaseModel):
    """Only height references may be replaced; credentials and flags are not file inputs."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1]
    height_references: list[HeightReference] = Field(max_length=100)
    simulation_height_references: list[SimulationHeightReference] = Field(max_length=100)


_cached: tuple[Settings, tuple[object, ...], HeightReferenceDocument] | None = None


def read_reference_file(path: Path, settings: Settings) -> HeightReferenceDocument:
    """Read a private regular file and reuse all application assignment/simulation guards.

    Open before inspecting metadata to avoid following a replaced symlink. Atomic rename
    gives concurrent requests either complete document; each logical operation pins its
    selected references separately. A failed replacement does not return the cached file.
    """
    global _cached
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or not 0 < info.st_size <= MAX_REFERENCE_FILE_BYTES
            ):
                raise ReferenceFileError("Height reference file must be private and bounded")
            signature = (
                str(path),
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_size,
            )
            cached = _cached
            if cached is not None and cached[0] is settings and cached[1] == signature:
                return cached[2]
            raw = stream.read(MAX_REFERENCE_FILE_BYTES + 1)
        if len(raw) > MAX_REFERENCE_FILE_BYTES:
            raise ReferenceFileError("Height reference file exceeds its size limit")
        document = HeightReferenceDocument.model_validate_json(raw)
        Settings.model_validate(
            {
                **settings.model_dump(),
                "EDGE_V2_HEIGHT_REFERENCES": document.height_references,
                "EDGE_V2_SIMULATION_HEIGHT_REFERENCES": document.simulation_height_references,
            }
        )
    except (OSError, ValidationError, ValueError):
        # Never attach a private path or the original JSON/validation input to this error.
        raise ReferenceFileError("Height reference file is unavailable or invalid") from None
    _cached = (settings, signature, document)
    return document


def configured_references(
    settings: Settings,
) -> tuple[HeightReference | SimulationHeightReference, ...]:
    """A configured but invalid file fails closed; environment references are not fallback."""
    if settings.EDGE_V2_HEIGHT_REFERENCE_FILE is None:
        return (*settings.EDGE_V2_HEIGHT_REFERENCES, *settings.EDGE_V2_SIMULATION_HEIGHT_REFERENCES)
    try:
        document = read_reference_file(settings.EDGE_V2_HEIGHT_REFERENCE_FILE, settings)
    except ReferenceFileError:
        return ()
    return (*document.height_references, *document.simulation_height_references)
