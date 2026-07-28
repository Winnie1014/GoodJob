"""Writer-entry recovery for crash-interrupted runtime publications."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

from goodjob.errors import InvalidInputError
from goodjob.paths import DataPaths
from goodjob.process_identity import owner_process_stopped
from goodjob.safe_fs import SafeDataTree


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _owned_export_path(
    tree: SafeDataTree,
    relative: str,
    expected: tuple[str, ...],
) -> str:
    parts = tree.relative_parts(relative)
    if parts != expected or PurePosixPath(relative).is_absolute():
        raise InvalidInputError("ExportAttempt path is outside its registered ownership")
    return relative


def recover_interrupted_exports(
    connection: sqlite3.Connection,
    paths: DataPaths,
) -> None:
    """Mark provably dead export owners and clean only their registered paths."""
    tree = SafeDataTree(
        paths.root,
        "exports",
        "export",
        frozenset({("exports", ".tmp")}),
    )
    cleanup: list[tuple[str, str, str]] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            """
            SELECT ea.export_attempt_id, ea.derived_export_id,
                   ea.temp_relative_path, ea.final_relative_path,
                   ea.owner_process_identity, ea.status
            FROM export_attempts AS ea
            LEFT JOIN derived_exports AS de
              ON de.export_attempt_id = ea.export_attempt_id
            WHERE ea.status IN ('running', 'failed', 'interrupted')
              AND de.derived_export_id IS NULL
            ORDER BY ea.started_at, ea.export_attempt_id
            """
        ).fetchall()
        for row in rows:
            status = str(row["status"])
            if status == "running" and not owner_process_stopped(
                str(row["owner_process_identity"])
            ):
                continue
            attempt_id = str(row["export_attempt_id"])
            export_id = str(row["derived_export_id"])
            temp_relative = _owned_export_path(
                tree,
                str(row["temp_relative_path"]),
                ("exports", ".tmp", attempt_id),
            )
            final_relative = _owned_export_path(
                tree,
                str(row["final_relative_path"]),
                ("exports", export_id),
            )
            cleanup.append((attempt_id, temp_relative, final_relative))
            if status == "running":
                connection.execute(
                    """
                    UPDATE export_attempts
                    SET status = 'interrupted', finished_at = ?,
                        error_summary =
                            'owner process stopped before publication completed'
                    WHERE export_attempt_id = ? AND status = 'running'
                    """,
                    (_now(), attempt_id),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    for attempt_id, temp_relative, final_relative in cleanup:
        tree.remove(temp_relative)
        if (
            connection.execute(
                "SELECT 1 FROM derived_exports WHERE export_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            is None
        ):
            tree.remove(final_relative)
