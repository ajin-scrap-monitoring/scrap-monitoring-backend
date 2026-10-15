"""Read-only checks before adopting legacy DBs or starting a migrated application."""

from sqlalchemy import CheckConstraint, MetaData, UniqueConstraint, inspect
from sqlalchemy.engine import Connection


class SchemaStateError(RuntimeError):
    """Safe diagnostic that contains no database URLs, SQL values or credentials."""


def application_tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names()) - {"alembic_version"}


def validate_schema(connection: Connection, expected: MetaData, *, complete: bool = False) -> None:
    inspector = inspect(connection)
    present = application_tables(connection)
    if present - set(expected.tables):
        raise SchemaStateError("Unexpected tables found; use a dedicated backend database")
    if complete and present != set(expected.tables):
        raise SchemaStateError("Database tables are incomplete; apply migrations before startup")
    for table in expected.sorted_tables:
        if table.name not in present:
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()):
            raise SchemaStateError(
                f"Column mismatch in {table.name}; a reviewed migration is required"
            )
        for column in table.columns:
            actual = columns[column.name]
            actual_type = actual["type"]
            if (
                actual_type._type_affinity is not column.type._type_affinity
                or getattr(actual_type, "length", None) != getattr(column.type, "length", None)
                or actual["nullable"] != column.nullable
                or (
                    connection.dialect.name == "postgresql"
                    and getattr(actual_type, "timezone", None)
                    != getattr(column.type, "timezone", None)
                )
            ):
                raise SchemaStateError(f"Column definition mismatch in {table.name}")
        primary_key = inspector.get_pk_constraint(table.name)["constrained_columns"]
        if primary_key != [column.name for column in table.primary_key.columns]:
            raise SchemaStateError(f"Primary key mismatch in {table.name}")
        unique: set[tuple[str | None, ...]] = {
            tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)
        }
        indexes = inspector.get_indexes(table.name)
        unique.update(tuple(item["column_names"]) for item in indexes if item["unique"])
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                names = tuple(column.name for column in constraint.columns)
                if names not in unique:
                    raise SchemaStateError(f"Missing uniqueness constraint in {table.name}")
        foreign_keys = {
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys(table.name)
        }
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            key = (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
            )
            if key not in foreign_keys:
                raise SchemaStateError(f"Missing foreign key in {table.name}")
        checks = {item["name"] for item in inspector.get_check_constraints(table.name)}
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name not in checks:
                raise SchemaStateError(f"Missing check constraint in {table.name}")
        existing_indexes = {item["name"]: item for item in indexes}
        for index in table.indexes:
            if index.unique and tuple(column.name for column in index.columns) not in unique:
                raise SchemaStateError(f"Missing unique index in {table.name}")
            if index.name in existing_indexes:
                actual_index = existing_indexes[index.name]
                if actual_index["column_names"] != [
                    column.name for column in index.columns
                ] or bool(actual_index["unique"]) != bool(index.unique):
                    raise SchemaStateError(f"Index mismatch in {table.name}")
            elif complete:
                raise SchemaStateError(f"Missing index in {table.name}")
