"""Receipt / outbox store (SQLite via the standard library).

Every write is recorded here *before* the backend is called, so a restart
between remote acceptance and local acknowledgement leaves a reconcilable row.
``":memory:"`` (the default when no path is configured) keeps nothing across
processes and says so through ``durable = False``: never claim durable capture
from in-memory mode.
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
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM receipts WHERE scope_key = ? AND content_hash = ? "
                "AND status NOT IN ('failed', 'deleted') ORDER BY created_at DESC LIMIT 1",
                (scope_key, content_hash),
            ).fetchone()
        return self._row(row, self.durable) if row else None

    def update(self, record_id: str, **fields: object) -> Optional[Receipt]:
        allowed = {"status", "dataset_id", "data_id", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown receipt fields: {sorted(unknown)}")
        sets = ["updated_at = ?"]
        params: list[object] = [_now()]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value.value if isinstance(value, ReceiptStatus) else value)
        params.append(record_id)
        with self._lock:
            self._db.execute(f"UPDATE receipts SET {', '.join(sets)} WHERE record_id = ?", params)
            self._db.commit()
        return self.get(record_id)

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
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM receipts WHERE status IN ({placeholders}) "
                "ORDER BY created_at ASC LIMIT ?",
                tuple(s.value for s in PENDING_STATUSES) + (limit,),
            ).fetchall()
        return [self._row(row, self.durable) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM receipts GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}
