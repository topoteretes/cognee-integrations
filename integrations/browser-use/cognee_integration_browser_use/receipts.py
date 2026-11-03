"""Receipt / outbox store (SQLite via the standard library).

Every write is recorded here *before* the backend is called, so a restart
between remote acceptance and local acknowledgement leaves a reconcilable row.
``":memory:"`` (the default when no path is configured) keeps nothing across
processes and says so through ``durable = False``: never claim durable capture
from in-memory mode.

The store is synchronous and thread-safe (one connection behind a lock).
``CogneeBrowserMemory`` runs it on a dedicated worker thread so commits never
block the agent's event loop; host code may call it directly.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import PENDING_STATUSES, Receipt, ReceiptStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
  record_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  dataset TEXT NOT NULL,
  upload_name TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  dataset_id TEXT,
  data_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_scope ON receipts (scope_key, content_hash);
CREATE INDEX IF NOT EXISTS receipts_status ON receipts (status);
"""

_COLUMNS = (
    "record_id, run_id, scope_key, dataset, upload_name, content_hash, kind, status, "
    "dataset_id, data_id, error, created_at, updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReceiptStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or ":memory:"
        self.durable = self.path != ":memory:"
        if self.durable:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.durable:
            self._db.execute("PRAGMA journal_mode = WAL")
            # Under WAL, NORMAL still survives a process crash (the WAL is synced
            # at checkpoints); it stops every commit from paying an fsync.
            self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row: sqlite3.Row, durable: bool) -> Receipt:
        return Receipt(
            record_id=row["record_id"],
            run_id=row["run_id"],
            scope_key=row["scope_key"],
            dataset=row["dataset"],
            upload_name=row["upload_name"],
            content_hash=row["content_hash"],
            kind=row["kind"],
            status=ReceiptStatus(row["status"]),
            dataset_id=row["dataset_id"],
            data_id=row["data_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            durable=durable,
        )

    def create(
        self,
        *,
        record_id: str,
        run_id: str,
        scope_key: str,
        dataset: str,
        upload_name: str,
        content_hash: str,
        kind: str,
    ) -> Receipt:
        now = _now()
        with self._lock:
            self._db.execute(
                f"INSERT INTO receipts ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    run_id,
                    scope_key,
                    dataset,
                    upload_name,
                    content_hash,
                    kind,
                    ReceiptStatus.ACCEPTED.value,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            self._db.commit()
        receipt = self.get(record_id)
        assert receipt is not None
        return receipt

    def get(self, record_id: str) -> Optional[Receipt]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM receipts WHERE record_id = ?", (record_id,)
            ).fetchone()
        return self._row(row, self.durable) if row else None

    def find_live_by_hash(self, scope_key: str, content_hash: str) -> Optional[Receipt]:
        """The receipt that makes a write with this content redundant, if any.

        Only a write known to have landed counts (``accepted``, ``indexing``,
        ``ready``). ``failed`` and ``deleted`` are gone, and ``unknown`` means the
        write timed out, was cancelled, or its document never appeared: none of
        those may block the same content from being stored again. Re-uploading is
        safe because identical content serializes to identical bytes under the
        same content-derived upload name, so the server stores one document.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM receipts WHERE scope_key = ? AND content_hash = ? "
                "AND status IN ('accepted', 'indexing', 'ready') "
                "ORDER BY created_at DESC LIMIT 1",
                (scope_key, content_hash),
            ).fetchone()
        return self._row(row, self.durable) if row else None

    @staticmethod
    def _assignment(fields: dict[str, object]) -> tuple[str, list[object]]:
        allowed = {"status", "dataset_id", "data_id", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown receipt fields: {sorted(unknown)}")
        sets = ["updated_at = ?"]
        params: list[object] = [_now()]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value.value if isinstance(value, ReceiptStatus) else value)
        return ", ".join(sets), params

    def update(self, record_id: str, **fields: object) -> Optional[Receipt]:
        assignment, params = self._assignment(fields)
        with self._lock:
            self._db.execute(
                f"UPDATE receipts SET {assignment} WHERE record_id = ?", [*params, record_id]
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT * FROM receipts WHERE record_id = ?", (record_id,)
            ).fetchone()
        return self._row(row, self.durable) if row else None

    def update_many(self, updates: list[tuple[str, dict[str, object]]]) -> list[Optional[Receipt]]:
        """Apply several updates in one transaction (one commit, one fsync at most).

        Returns the refreshed receipts in input order (``None`` for unknown ids).
        """
        statements = [(record_id, *self._assignment(fields)) for record_id, fields in updates]
        out: list[Optional[Receipt]] = []
        with self._lock:
            for record_id, assignment, params in statements:
                self._db.execute(
                    f"UPDATE receipts SET {assignment} WHERE record_id = ?", [*params, record_id]
                )
            self._db.commit()
            for record_id, _, _ in statements:
                row = self._db.execute(
                    "SELECT * FROM receipts WHERE record_id = ?", (record_id,)
                ).fetchone()
                out.append(self._row(row, self.durable) if row else None)
        return out

    def list(self, *, status: Optional[ReceiptStatus] = None, limit: int = 100) -> list[Receipt]:
        with self._lock:
            if status is None:
                rows = self._db.execute(
                    "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM receipts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [self._row(row, self.durable) for row in rows]

    def list_pending(self, limit: int = 500) -> list[Receipt]:
        """Pending receipts, the ones most worth checking first.

        Receipts that look live (``accepted``, ``indexing``) come before ``unknown``
        ones, and within each group the least recently checked comes first, so a
        backlog of stale ``unknown`` rows can never starve newer receipts of their
        refresh (each call moves the rows it checked to the back of the queue).
        """
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM receipts WHERE status IN ({placeholders}) "
                "ORDER BY CASE WHEN status = ? THEN 1 ELSE 0 END, updated_at ASC, created_at ASC "
                "LIMIT ?",
                tuple(s.value for s in PENDING_STATUSES) + (ReceiptStatus.UNKNOWN.value, limit),
            ).fetchall()
        return [self._row(row, self.durable) for row in rows]

    def mark_deleted_by_content(
        self, scope_key: str, content_hash: str, dataset: str, *, except_record_id: str
    ) -> list[str]:
        """Mark every other receipt for this content in this dataset ``deleted``.

        Receipts of identical content share one content-derived upload name and
        therefore one remote document; once a complete listing shows nothing of
        that name, all of them are gone. ``failed`` and ``deleted`` rows are left
        alone. Returns the record ids that changed, in one transaction.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT record_id FROM receipts WHERE scope_key = ? AND content_hash = ? "
                "AND dataset = ? AND record_id != ? AND status NOT IN (?, ?)",
                (
                    scope_key,
                    content_hash,
                    dataset,
                    except_record_id,
                    ReceiptStatus.DELETED.value,
                    ReceiptStatus.FAILED.value,
                ),
            ).fetchall()
            ids = [row["record_id"] for row in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self._db.execute(
                    f"UPDATE receipts SET status = ?, error = NULL, updated_at = ? "
                    f"WHERE record_id IN ({marks})",
                    (ReceiptStatus.DELETED.value, _now(), *ids),
                )
                self._db.commit()
        return ids

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM receipts GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}
