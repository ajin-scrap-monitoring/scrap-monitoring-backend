"""Versioned legacy-history index definition; durable receipt keys stay unchanged."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

INDEX = "ix_v2_legacy_history"
STAGING = "ix_v2_legacy_history_partial_v1"
COLUMNS = "site_id, edge_id, message_type, compact, generated_at"
PREDICATE = "compact IS FALSE"
PREVIOUS = "0008_operations_recipient"


def index_state(
    connection: Connection, name: str, allow_full: bool = False
) -> tuple[bool, bool] | None:
    """Reject a foreign/malformed index rather than replacing it on a retry."""
    row = (
        connection.execute(
            text("""
        SELECT t.relname AS tablename, i.indisvalid, i.indisready, i.indisunique,
               am.amname, pg_get_expr(i.indpred,i.indrelid) AS predicate,
               ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
                     JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
                     ORDER BY k.ord) AS columns
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_class t ON t.oid=i.indrelid
        JOIN pg_am am ON am.oid=c.relam
        WHERE n.nspname=current_schema() AND c.relname=:name
    """),
            {"name": name},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    correct = (
        row["tablename"] == "edge_v2_messages"
        and row["amname"] == "btree"
        and not row["indisunique"]
        and row["columns"] == COLUMNS.split(", ")
        and (row["predicate"] == "(compact IS FALSE)" or allow_full and row["predicate"] is None)
    )
    return bool(correct), bool(row["indisvalid"] and row["indisready"])
