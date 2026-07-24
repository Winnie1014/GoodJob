"""SQLite migrations and transaction boundaries for owner-local state."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from goodjob.errors import UnsupportedSchemaError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="authorization_receipts",
        statements=(
            """
            CREATE TABLE authorization_receipts (
                authorization_receipt_id TEXT PRIMARY KEY,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'source_analysis',
                        'external_git_relation_probe',
                        'external_git_metadata'
                    )
                ),
                session_binding_digest BLOB NOT NULL,
                issuer_kind TEXT NOT NULL CHECK (issuer_kind = 'codex_task_runtime'),
                scope_descriptor TEXT NOT NULL,
                notice_version TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX authorization_receipts_scope_idx
            ON authorization_receipts(receipt_kind, scope_descriptor, notice_version)
            """,
        ),
    ),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    """Versioned SQLite access with one non-blocking writer at a time."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def migrate(self) -> int:
        """Apply all known migrations, rejecting a database from a newer runtime."""
        self.paths.ensure_layout()
        with ExclusiveWriterLock(self.paths.writer_lock_file):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                current_version = int(row["version"])
                latest_version = MIGRATIONS[-1].version
                if current_version > latest_version:
                    raise UnsupportedSchemaError(
                        "database schema is newer than this installed GoodJob version"
                    )
                for migration in MIGRATIONS:
                    if migration.version <= current_version:
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                    current_version = migration.version
                connection.commit()
                return current_version
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def write_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a short atomic write transaction after applying known schema changes."""
        self.migrate()
        with ExclusiveWriterLock(self.paths.writer_lock_file):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a parallel read connection without acquiring the writer lock."""
        if not self.paths.database_file.exists():
            raise UnsupportedSchemaError("database is not initialized; run bootstrap first")
        connection = self._connect()
        try:
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise UnsupportedSchemaError(
                    "database is not initialized; run bootstrap first"
                ) from exc
            if int(row["version"]) != MIGRATIONS[-1].version:
                raise UnsupportedSchemaError("database must be migrated before this read operation")
            yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.paths.database_file)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        return connection
