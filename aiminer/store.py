"""Durable correlation ledger.

The same correlation id and the same canonical value return the original
receipt. The same id with a different canonical value is a conflict and does
not replace the stored row. A canon value that is not a blob, or a receipt
that is not the sha256 of that blob, is corrupt and does not leave the
write transaction open. A blob that hashes to its receipt but is not
canonical JSON, such as ``1.0``, is corrupt rather than a conflict.
Omitting a correlation id is not this API; an
empty id is rejected. The id is not trimmed and is not NFKC-folded.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from aiminer.canon import canonical_bytes
from aiminer.errors import CanonError, ScanError, StoreError
from aiminer.ids import check_label
from aiminer.limits import Limits, check_max_entries
from aiminer.scan import parse_json

# Highest legal caps. Re-reading a stored blob must not depend on the
# caller's tighter caps, and must still reject a non-canonical document.
_AUDIT_LIMITS = Limits(
    max_depth=64,
    max_string=1_000_000,
    max_keys=100_000,
    max_items=100_000,
    max_nodes=1_000_000,
    max_number_literal=10_000,
    max_exponent=100_000,
    max_chars=5_000_000,
)


@dataclass(frozen=True)
class Stored:
    ok: bool
    code: str
    receipt: str | None = None


class ReceiptStore:
    """SQLite ledger. One process may share it across threads."""

    def __init__(self, path: str | Path, *, max_entries: int = 4096) -> None:
        self._max_entries = check_max_entries(max_entries)
        if isinstance(path, Path):
            location: str | Path = path
        elif isinstance(path, str):
            location = path
        else:
            raise ValueError("path")
        if location == "":
            raise ValueError("path")
        try:
            as_text = str(location)
        except ValueError as exc:
            raise StoreError("path", "unopenable") from exc
        if "\x00" in as_text:
            raise StoreError("path", "nul")
        if location != ":memory:":
            file_path = Path(location)
            if file_path.exists() and not file_path.is_file():
                raise StoreError("path", "not a file")
        try:
            connection = sqlite3.connect(str(location), check_same_thread=False, isolation_level=None)
        except (sqlite3.OperationalError, ValueError) as exc:
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
        # notnull is 0 on the text primary key: SQLite records it that way.
        # canon and receipt are NOT NULL. A nullable canon is corrupt.
        found = [(row[1], str(row[2]).upper(), int(row[3]), int(row[5])) for row in info]
        expected = [
            ("correlation_id", "TEXT", 0, 1),
            ("canon", "BLOB", 1, 0),
            ("receipt", "TEXT", 1, 0),
        ]
        if found != expected:
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
                    count = connection.execute("SELECT COUNT(*) FROM receipts").fetchone()
                    if count is None or int(count[0]) >= self._max_entries:
                        connection.execute("ROLLBACK")
                        if count is None:
                            raise StoreError("corrupt", "count")
                        return Stored(False, "too_many_entries", None)
                    connection.execute(
                        "INSERT INTO receipts(correlation_id, canon, receipt) VALUES (?, ?, ?)",
                        (correlation_id, canon, digest),
                    )
                    connection.execute("COMMIT")
                    return Stored(True, "created", digest)
                stored = _canon_bytes(row[0])
                receipt = row[1]
                # An integer or text value in the blob column is not a conflict.
                # bytes(5) is five NUL bytes, and bytes(str) raises TypeError
                # and used to leave this transaction open.
                if not isinstance(receipt, str) or hashlib.sha256(stored).hexdigest() != receipt:
                    raise StoreError("corrupt", "receipt")
                _audit_canon(stored)
                if stored == canon:
                    connection.execute("COMMIT")
                    return Stored(True, "duplicate", receipt)
                connection.execute("ROLLBACK")
                return Stored(False, "conflict", None)
            except StoreError:
                self._rollback(connection)
                raise
            except sqlite3.DatabaseError as exc:
                self._rollback(connection)
                raise StoreError("corrupt", "write") from exc
            except Exception:
                self._rollback(connection)
                raise

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


def _canon_bytes(value: object) -> bytes:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value
    raise StoreError("corrupt", "canon")


def _audit_canon(stored: bytes) -> None:
    try:
        text = stored.decode("utf-8")
        parsed = parse_json(text, _AUDIT_LIMITS)
        again = canonical_bytes(parsed, _AUDIT_LIMITS)
    except (UnicodeDecodeError, ScanError, CanonError) as exc:
        raise StoreError("corrupt", "canon") from exc
    if again != stored:
        raise StoreError("corrupt", "canon")
