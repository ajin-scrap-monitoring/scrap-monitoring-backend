"""Versioned SQL for migration-managed raw-retention lookup indexes.

Keep these expressions stable: the runtime predicates must match the PostgreSQL
partial/expression indexes without parameterized JSON paths or substring lengths.
"""

from dataclasses import dataclass

ARTIFACT_INDEX = "ix_edge_v2_raw_artifact_retention"
MESSAGE_INDEX = "ix_edge_v2_raw_message_retention"
INDEX_NAMES = frozenset({ARTIFACT_INDEX, MESSAGE_INDEX})


@dataclass(frozen=True)
class RetentionSQL:
    artifact_time: str
    artifact_day: str
    artifact_where: str
    message_where: str


def retention_sql(dialect: str) -> RetentionSQL:
    if dialect == "postgresql":
        acquired = "payload->>'acquired_at'"
        scan_time = "payload->'scan'->>'acquired_at'"
        generated = "payload->>'generated_at'"
        marker = "payload->>'retention'"
        assessment = "payload->>'assessment_type'"
        cloud = "payload->>'filtered_point_clouds'"
    elif dialect == "sqlite":
        acquired = "json_extract(payload, '$.acquired_at')"
        scan_time = "json_extract(payload, '$.scan.acquired_at')"
        generated = "json_extract(payload, '$.generated_at')"
        marker = "json_extract(payload, '$.retention')"
        assessment = "json_extract(payload, '$.assessment_type')"
        cloud = "json_extract(payload, '$.filtered_point_clouds')"
    else:
        raise ValueError("Raw retention supports PostgreSQL and SQLite only")
    timestamp = (
        f"CASE WHEN kind = 'scan_source' THEN {acquired} "
        f"WHEN kind = 'scan_result' THEN {scan_time} ELSE {generated} END"
    )
    state_cloud = f"{assessment} = 'STATE' AND {cloud} IS NOT NULL"
    return RetentionSQL(
        artifact_time=timestamp,
        artifact_day=f"substr({timestamp}, 1, 10)",
        artifact_where=(
            f"((kind IN ('scan_source', 'scan_result') AND {marker} IS NULL) OR "
            f"(kind = 'assessment_revision' AND {state_cloud}))"
        ),
        message_where=(
            f"((message_type = 'lidar_measurement' AND "
            f"({marker} IS NULL OR {marker} <> 'RAW_EXPIRED')) OR "
            f"(message_type = 'edge_assessment' AND {state_cloud}))"
        ),
    )
