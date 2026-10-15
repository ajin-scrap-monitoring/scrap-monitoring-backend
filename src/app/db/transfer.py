"""Offline, bounded-memory transfer from read-only SQLite to an empty PostgreSQL DB."""

import hashlib
import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, String, Table, func, insert, null, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from src.app.db.base import Base
from src.app.db.migration import require_current_schema
from src.app.db.schema_guard import SchemaStateError

SQL_NULL = object()


class TransferError(RuntimeError):
    """Safe error with no source rows, SQL parameters or credentials."""


@dataclass(frozen=True)
class TableSummary:
    rows: int
    digest: str


def sqlite_url(path: Path) -> URL:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise TransferError("Source must be an existing SQLite backup file")
    return URL.create(
        "sqlite+aiosqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"}
    )


def normalized(value: Any) -> Any:
    if value is SQL_NULL:
        return ["sql-null"]
    if isinstance(value, datetime):
        # SQLite drops tzinfo for the application's UTC columns. Never treat
        # those stored values as the workstation's local time.
        return ["utc", value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TransferError("Non-finite numeric value found; review source data")
        return ["float", value.hex()]
    return ["value", value]


def row_bytes(row: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            {key: normalized(value) for key, value in row.items()},
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        raise TransferError("Unsupported data value found; review source data") from None


async def batches(
    connection: AsyncConnection, table: Table, batch_size: int
) -> AsyncIterator[list[dict[str, Any]]]:
    json_columns = [column for column in table.columns if isinstance(column.type, JSON)]
    order = [
        column.collate("C" if connection.dialect.name == "postgresql" else "BINARY")
        if isinstance(column.type, String)
        else column
        for column in table.primary_key.columns
    ]
    statement = select(
        *table.columns,
        *(column.is_(None).label("_sql_null_" + column.name) for column in json_columns),
    ).order_by(*order)
    async with connection.stream(statement) as result:
        async for partition in result.mappings().partitions(batch_size):
            batch = []
            for original in partition:
                row = dict(original)
                for column in json_columns:
                    if row.pop("_sql_null_" + column.name):
                        row[column.name] = SQL_NULL
                for column in table.columns:
                    value = row[column.name]
                    if isinstance(column.type, DateTime) and isinstance(value, datetime):
                        row[column.name] = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
                batch.append(row)
            yield batch


async def summarize(connection: AsyncConnection, table: Table, batch_size: int) -> TableSummary:
    count = 0
    digest = hashlib.sha256()
    async for batch in batches(connection, table, batch_size):
        for row in batch:
            encoded = row_bytes(row)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    return TableSummary(count, digest.hexdigest())


async def copy_table(
    source: AsyncConnection, destination: AsyncConnection, table: Table, batch_size: int
) -> None:
    async for batch in batches(source, table, batch_size):
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in batch:
            row_bytes(row)  # Reject values that cannot be compared safely before inserting.
            missing = tuple(key for key, value in row.items() if value is SQL_NULL)
            values = {
                key: JSON.NULL if value is None and isinstance(table.c[key].type, JSON) else value
                for key, value in row.items()
                if value is not SQL_NULL
            }
            groups.setdefault(missing, []).append(values)
        for missing, rows in groups.items():
            # Preserve SQL NULL separately from the JSON literal null. Supplying
            # every column also prevents ORM defaults from replacing stored values.
            await destination.execute(
                insert(table).values({name: null() for name in missing}), rows
            )


async def compare(
    source: AsyncConnection, destination: AsyncConnection, batch_size: int
) -> dict[str, TableSummary]:
    summaries = {}
    for table in Base.metadata.sorted_tables:
        expected = await summarize(source, table, batch_size)
        actual = await summarize(destination, table, batch_size)
        if expected != actual:
            raise TransferError(f"Data comparison failed in {table.name}; no changes saved")
        summaries[table.name] = expected
    return summaries


async def restart_sequences(connection: AsyncConnection) -> None:
    quote = connection.dialect.identifier_preparer.quote
    schema = await connection.scalar(text("SELECT current_schema()"))
    if not isinstance(schema, str):
        raise TransferError("Destination schema is unavailable")
    for table in Base.metadata.sorted_tables:
        column = table.autoincrement_column
        if column is None:
            continue
        sequence = await connection.scalar(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": f"{quote(schema)}.{quote(table.name)}", "column_name": column.name},
        )
        if not sequence:
            raise TransferError(f"ID sequence missing in {table.name}")
        record = (
            await connection.execute(
                text(
                    "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE c.oid = to_regclass(:sequence)"
                ),
                {"sequence": sequence},
            )
        ).one()
        maximum = await connection.scalar(select(func.max(column)))
        next_id = max(1, int(maximum or 0) + 1)
        # Unlike setval(), RESTART rolls back along with a failed/test transfer.
        await connection.execute(
            text(
                f"ALTER SEQUENCE {quote(record.nspname)}.{quote(record.relname)} "
                f"RESTART WITH {next_id}"
            )
        )


async def transfer_connections(
    source: AsyncConnection, destination: AsyncConnection, *, mode: str, batch_size: int
) -> dict[str, TableSummary]:
    from src.app import models  # noqa: F401 -- register all application tables

    if source.dialect.name != "sqlite" or destination.dialect.name != "postgresql":
        raise TransferError("Only SQLite to PostgreSQL transfer is supported")
    await source.run_sync(require_current_schema)
    await destination.execute(text("SET LOCAL lock_timeout = '5s'"))
    await destination.run_sync(require_current_schema)
    if (await source.exec_driver_sql("PRAGMA quick_check")).scalars().all() != ["ok"]:
        raise TransferError("SQLite integrity check failed")
    if (await source.exec_driver_sql("PRAGMA foreign_key_check")).first() is not None:
        raise TransferError("SQLite foreign-key check failed; review source data")
    names = ["alembic_version", *sorted(Base.metadata.tables)]
    quote = destination.dialect.identifier_preparer.quote
    # Exclude readers/writers for the complete destination transaction. Deployment
    # still requires stopping every producer before taking the source backup.
    await destination.execute(
        text("LOCK TABLE " + ", ".join(quote(name) for name in names) + " IN ACCESS EXCLUSIVE MODE")
    )
    await destination.run_sync(require_current_schema)
    if mode != "verify":
        for table in Base.metadata.sorted_tables:
            if await destination.scalar(select(table).limit(1)) is not None:
                raise TransferError("Destination contains data; merging/overwriting is forbidden")
        for table in Base.metadata.sorted_tables:
            await copy_table(source, destination, table, batch_size)
    summaries = await compare(source, destination, batch_size)
    if mode != "verify":
        await restart_sequences(destination)
    return summaries


async def transfer_database(
    source_path: Path, destination_url: str | URL, *, mode: str = "check", batch_size: int = 50
) -> dict[str, TableSummary]:
    """Check rolls back; apply commits only after comparison; verify never writes."""
    if mode not in ("check", "apply", "verify") or not 1 <= batch_size <= 500:
        raise TransferError("Invalid transfer mode or batch size")
    try:
        target = make_url(destination_url)
        if target.drivername != "postgresql+asyncpg":
            raise TransferError("Destination must use postgresql+asyncpg")
        source_engine = create_async_engine(sqlite_url(source_path), hide_parameters=True)
        try:
            destination_engine = create_async_engine(target, hide_parameters=True)
            try:
                async with source_engine.connect() as source, destination_engine.connect() as dest:
                    # Explicit BEGIN is required for a real SQLite read snapshot
                    # with the sqlite3 driver's legacy transaction behavior.
                    await source.exec_driver_sql("BEGIN")
                    async with dest.begin() as transaction:
                        summaries = await transfer_connections(
                            source, dest, mode=mode, batch_size=batch_size
                        )
                        if mode != "apply":
                            await transaction.rollback()
                    return summaries
            finally:
                await destination_engine.dispose()
        finally:
            await source_engine.dispose()
    except (TransferError, SchemaStateError):
        raise
    except Exception:
        raise TransferError(
            "Transfer failed; no success confirmed. Check DB state before retrying"
        ) from None
