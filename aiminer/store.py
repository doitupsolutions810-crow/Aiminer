"""Durable correlation ledger.

The same correlation id and the same canonical value return the original
receipt. The same id with a different canonical value is a conflict and does
not replace the stored row. Omitting a correlation id is not this API; an
empty id is rejected. The id is not trimmed and is not NFKC-folded.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from aiminer.canon import canonical_bytes
from aiminer.errors import CanonError, StoreError
from aiminer.ids import check_label
from aiminer.limits import Limits


@dataclass(frozen=True)
class Stored:
    ok: bool
    code: str
    receipt: str | None = None


class ReceiptStore:
    """SQLite ledger. One process may share it across threads."""

    def __init__(self, path: str | Path) -> None:
        if isinstance(path, Path):
            location: str | Path = path
        elif isinstance(path, str):
            location = path
        else:
            raise ValueError("path")
        if location == "":
            raise ValueError("path")
        if location != ":memory:":
            file_path = Path(location)
            if file_path.exists() and not file_path.is_file():
                raise StoreError("path", "not a file")
        try:
            connection = sqlite3.connect(str(location), check_same_thread=False, isolation_level=None)
        except sqlite3.OperationalError as exc:
            raise StoreError("path", "unopenable") from exc
        try:
            self._prepare(connection)
        except Exception:
            connection.close()
            raise
        self._conn: sqlite3.Connection | None = connection
        self._lock = threading.Lock()

    def _prepare(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=3000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    correlation_id TEXT PRIMARY KEY,
                    canon BLOB NOT NULL,
                    receipt TEXT NOT NULL
                )
                """
            )
            info = connection.execute("PRAGMA table_info(receipts)").fetchall()
        except sqlite3.DatabaseError as exc:
            raise StoreError("corrupt", "database") from exc
        columns = [row[1] for row in info]
        if columns != ["correlation_id", "canon", "receipt"]:
            raise StoreError("corrupt", "schema")

    def __enter__(self) -> "ReceiptStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            connection = self._conn
            self._conn = None
            if connection is not None:
                connection.close()

    def count(self) -> int:
        with self._lock:
            connection = self._require()
            row = connection.execute("SELECT COUNT(*) FROM receipts").fetchone()
            return int(row[0])

    def submit(self, correlation_id: object, value: object, limits: Limits | None = None) -> Stored:
        reason = check_label(correlation_id, what="correlation")
        if reason is not None:
            return Stored(False, reason, None)
        bounds = limits if limits is not None else Limits()
        if not isinstance(bounds, Limits):
            raise ValueError("limits")
        try:
            canon = canonical_bytes(value, bounds)
        except CanonError as exc:
            return Stored(False, exc.code, None)
        digest = hashlib.sha256(canon).hexdigest()
        assert isinstance(correlation_id, str)
        with self._lock:
            connection = self._require()
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.DatabaseError as exc:
                raise StoreError("corrupt", "begin") from exc
            try:
                row = connection.execute(
                    "SELECT canon, receipt FROM receipts WHERE correlation_id = ?",
                    (correlation_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO receipts(correlation_id, canon, receipt) VALUES (?, ?, ?)",
                        (correlation_id, canon, digest),
                    )
                    connection.execute("COMMIT")
                    return Stored(True, "created", digest)
                if bytes(row[0]) == canon:
                    connection.execute("COMMIT")
                    return Stored(True, "duplicate", row[1])
                connection.execute("ROLLBACK")
                return Stored(False, "conflict", None)
            except sqlite3.DatabaseError as exc:
                self._rollback(connection)
                raise StoreError("corrupt", "write") from exc

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("closed", "store")
        return self._conn

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            return
