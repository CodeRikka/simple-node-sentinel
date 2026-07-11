from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._connection = sqlite3.connect(
                self.path, check_same_thread=False, timeout=10
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS gpu_process_records (
                    id INTEGER PRIMARY KEY,
                    gpu_uuid TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    pid INTEGER NOT NULL,
                    process_started_at REAL NOT NULL,
                    username TEXT,
                    command TEXT,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    ended_at REAL,
                    UNIQUE(gpu_uuid, pid, process_started_at)
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY,
                    gpu_uuid TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    triggered_at REAL NOT NULL,
                    recovered_at REAL,
                    current_temperature REAL NOT NULL,
                    max_temperature REAL NOT NULL,
                    users_json TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS email_records (
                    id INTEGER PRIMARY KEY,
                    alert_id INTEGER,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    recipients_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY(alert_id) REFERENCES alerts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_process_ended
                    ON gpu_process_records(ended_at);
                CREATE INDEX IF NOT EXISTS idx_alert_recovered
                    ON alerts(recovered_at);
                CREATE INDEX IF NOT EXISTS idx_email_created
                    ON email_records(created_at);
                """
            )
            self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not open")
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def reconcile_gpu_processes(
        self, processes: Iterable[dict[str, Any]], observed_at: float | None = None
    ) -> None:
        observed = observed_at if observed_at is not None else time.time()
        active: set[tuple[str, int, float]] = set()
        with self._lock, self.connection:
            for process in processes:
                key = (
                    str(process["gpu_uuid"]),
                    int(process["pid"]),
                    float(process["started_at"]),
                )
                active.add(key)
                self.connection.execute(
                    """
                    INSERT INTO gpu_process_records (
                        gpu_uuid, gpu_index, pid, process_started_at, username,
                        command, first_seen_at, last_seen_at, ended_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(gpu_uuid, pid, process_started_at) DO UPDATE SET
                        gpu_index=excluded.gpu_index,
                        username=excluded.username,
                        command=excluded.command,
                        last_seen_at=excluded.last_seen_at,
                        ended_at=NULL
                    """,
                    (
                        key[0],
                        process["gpu_index"],
                        key[1],
                        key[2],
                        process.get("username"),
                        process.get("command", ""),
                        observed,
                        observed,
                    ),
                )
            rows = self.connection.execute(
                """
                SELECT gpu_uuid, pid, process_started_at
                FROM gpu_process_records WHERE ended_at IS NULL
                """
            ).fetchall()
            ended = [
                (observed, row["gpu_uuid"], row["pid"], row["process_started_at"])
                for row in rows
                if (row["gpu_uuid"], row["pid"], row["process_started_at"])
                not in active
            ]
            self.connection.executemany(
                """
                UPDATE gpu_process_records SET ended_at=?
                WHERE gpu_uuid=? AND pid=? AND process_started_at=?
                """,
                ended,
            )

    def create_alert(
        self,
        gpu: dict[str, Any],
        users: Iterable[str],
        temperature: float,
        triggered_at: float | None = None,
    ) -> int:
        created = triggered_at if triggered_at is not None else time.time()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO alerts (
                    gpu_uuid, gpu_index, triggered_at, current_temperature,
                    max_temperature, users_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    gpu["uuid"],
                    gpu["index"],
                    created,
                    temperature,
                    temperature,
                    json.dumps(sorted(set(users))),
                ),
            )
            return int(cursor.lastrowid)

    def update_alert(
        self,
        alert_id: int,
        users: Iterable[str],
        current_temperature: float,
        max_temperature: float,
        recovered_at: float | None = None,
    ) -> None:
        status = "recovered" if recovered_at is not None else "active"
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE alerts
                SET users_json=?, current_temperature=?, max_temperature=?,
                    recovered_at=?, status=?
                WHERE id=?
                """,
                (
                    json.dumps(sorted(set(users))),
                    current_temperature,
                    max_temperature,
                    recovered_at,
                    status,
                    alert_id,
                ),
            )

    def record_email(
        self,
        alert_id: int | None,
        kind: str,
        recipients: Iterable[str],
        status: str,
        error: str | None = None,
    ) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO email_records (
                    alert_id, kind, created_at, recipients_json, status, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    kind,
                    time.time(),
                    json.dumps(sorted(set(recipients))),
                    status,
                    error,
                ),
            )

    def list_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT a.*,
                    (SELECT status FROM email_records e
                     WHERE e.alert_id=a.id ORDER BY e.id DESC LIMIT 1)
                    AS email_status
                FROM alerts a ORDER BY triggered_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["users"] = json.loads(item.pop("users_json"))
            result.append(item)
        return result

    def cleanup(self, retention_days: int, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        with self._lock, self.connection:
            self.connection.execute(
                """
                DELETE FROM gpu_process_records
                WHERE ended_at IS NOT NULL AND ended_at < ?
                """,
                (cutoff,),
            )
            self.connection.execute(
                """
                DELETE FROM email_records
                WHERE created_at < ?
                """,
                (cutoff,),
            )
            self.connection.execute(
                """
                DELETE FROM alerts
                WHERE recovered_at IS NOT NULL AND recovered_at < ?
                """,
                (cutoff,),
            )
