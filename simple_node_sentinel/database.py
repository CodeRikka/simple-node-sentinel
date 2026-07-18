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
                CREATE TABLE IF NOT EXISTS gpu_fan_control_state (
                    gpu_uuid TEXT PRIMARY KEY,
                    mode TEXT NOT NULL CHECK(mode IN ('auto', 'manual')),
                    target_percent INTEGER,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_process_ended
                    ON gpu_process_records(ended_at);
                CREATE INDEX IF NOT EXISTS idx_alert_recovered
                    ON alerts(recovered_at);
                CREATE INDEX IF NOT EXISTS idx_email_created
                    ON email_records(created_at);
                CREATE TABLE IF NOT EXISTS system_metric_samples (
                    sampled_at REAL PRIMARY KEY,
                    cpu_usage_percent REAL,
                    cpu_temperature_celsius REAL,
                    memory_used_bytes INTEGER,
                    memory_total_bytes INTEGER,
                    memory_usage_percent REAL,
                    swap_used_bytes INTEGER,
                    swap_total_bytes INTEGER,
                    swap_usage_percent REAL,
                    load_1 REAL,
                    load_5 REAL,
                    load_15 REAL
                );
                CREATE TABLE IF NOT EXISTS gpu_metric_samples (
                    gpu_uuid TEXT NOT NULL,
                    sampled_at REAL NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    utilization_percent REAL,
                    memory_used_bytes INTEGER,
                    memory_total_bytes INTEGER,
                    temperature_celsius REAL,
                    fan_percent REAL,
                    power_watts REAL,
                    power_limit_watts REAL,
                    PRIMARY KEY(gpu_uuid, sampled_at)
                );
                CREATE TABLE IF NOT EXISTS disk_metric_samples (
                    mountpoint TEXT NOT NULL,
                    sampled_at REAL NOT NULL,
                    device TEXT NOT NULL,
                    filesystem TEXT NOT NULL,
                    used_bytes INTEGER,
                    total_bytes INTEGER,
                    available_bytes INTEGER,
                    usage_percent REAL,
                    PRIMARY KEY(mountpoint, sampled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_system_metrics_time
                    ON system_metric_samples(sampled_at);
                CREATE INDEX IF NOT EXISTS idx_gpu_metrics_time
                    ON gpu_metric_samples(sampled_at);
                CREATE INDEX IF NOT EXISTS idx_disk_metrics_time
                    ON disk_metric_samples(sampled_at);
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

    def ensure_fan_control_state(self, gpu_uuid: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO gpu_fan_control_state (
                    gpu_uuid, mode, target_percent, revision, updated_at
                ) VALUES (?, 'auto', NULL, 0, ?)
                """,
                (gpu_uuid, now),
            )
            row = self.connection.execute(
                "SELECT * FROM gpu_fan_control_state WHERE gpu_uuid=?",
                (gpu_uuid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("unable to create GPU fan control state")
        return dict(row)

    def get_fan_control_state(self, gpu_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM gpu_fan_control_state WHERE gpu_uuid=?",
                (gpu_uuid,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_fan_control_state(
        self,
        gpu_uuid: str,
        mode: str,
        target_percent: int | None,
        expected_revision: int,
        updated_at: float | None = None,
    ) -> dict[str, Any] | None:
        now = updated_at if updated_at is not None else time.time()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE gpu_fan_control_state
                SET mode=?, target_percent=?, revision=revision + 1, updated_at=?
                WHERE gpu_uuid=? AND revision=?
                """,
                (mode, target_percent, now, gpu_uuid, expected_revision),
            )
            if cursor.rowcount != 1:
                return None
            row = self.connection.execute(
                "SELECT * FROM gpu_fan_control_state WHERE gpu_uuid=?",
                (gpu_uuid,),
            ).fetchone()
        return dict(row) if row is not None else None

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
                WHERE id=? AND status='active'
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

    def list_active_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM alerts
                WHERE status='active' AND recovered_at IS NULL
                ORDER BY triggered_at
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["users"] = json.loads(item.pop("users_json"))
            result.append(item)
        return result

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

    def record_metric_snapshot(
        self,
        sampled_at: float,
        summary: dict[str, Any],
        gpus: Iterable[dict[str, Any]],
        disks: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        cpu = summary.get("cpu") or {}
        temperature = summary.get("cpu_temperature") or {}
        memory = summary.get("memory") or {}
        swap = summary.get("swap") or {}
        load = cpu.get("load_average") or {}
        gpu_rows = [
            (
                str(gpu["uuid"]),
                sampled_at,
                int(gpu["index"]),
                str(gpu.get("name") or "Unknown GPU"),
                gpu.get("utilization_percent"),
                gpu.get("memory_used_bytes"),
                gpu.get("memory_total_bytes"),
                gpu.get("temperature_celsius"),
                gpu.get("fan_percent"),
                gpu.get("power_watts"),
                gpu.get("power_limit_watts"),
            )
            for gpu in gpus
        ]
        disk_rows = [
            (
                str(disk["mountpoint"]),
                sampled_at,
                str(disk.get("device") or ""),
                str(disk.get("filesystem") or ""),
                disk.get("used_bytes"),
                disk.get("total_bytes"),
                disk.get("available_bytes"),
                disk.get("usage_percent"),
            )
            for disk in (disks or ())
        ]
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO system_metric_samples (
                    sampled_at, cpu_usage_percent, cpu_temperature_celsius,
                    memory_used_bytes, memory_total_bytes, memory_usage_percent,
                    swap_used_bytes, swap_total_bytes, swap_usage_percent,
                    load_1, load_5, load_15
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sampled_at,
                    cpu.get("usage_percent"),
                    temperature.get("max_celsius"),
                    memory.get("used_bytes"),
                    memory.get("total_bytes"),
                    memory.get("usage_percent"),
                    swap.get("used_bytes"),
                    swap.get("total_bytes"),
                    swap.get("usage_percent"),
                    load.get("1m"),
                    load.get("5m"),
                    load.get("15m"),
                ),
            )
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO gpu_metric_samples (
                    gpu_uuid, sampled_at, gpu_index, name,
                    utilization_percent, memory_used_bytes, memory_total_bytes,
                    temperature_celsius, fan_percent, power_watts,
                    power_limit_watts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                gpu_rows,
            )
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO disk_metric_samples (
                    mountpoint, sampled_at, device, filesystem, used_bytes,
                    total_bytes, available_bytes, usage_percent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                disk_rows,
            )

    def query_metric_history(
        self, since: float, until: float, max_points: int
    ) -> dict[str, Any]:
        bucket_seconds = max(1.0, (until - since) / max_points)
        parameters = (since, bucket_seconds, since, until)
        with self._lock:
            system_rows = self.connection.execute(
                """
                SELECT
                    CAST((sampled_at - ?) / ? AS INTEGER) AS bucket,
                    AVG(sampled_at) AS sampled_at,
                    AVG(cpu_usage_percent) AS cpu_usage_percent,
                    MAX(cpu_temperature_celsius) AS cpu_temperature_celsius,
                    AVG(memory_used_bytes) AS memory_used_bytes,
                    AVG(memory_total_bytes) AS memory_total_bytes,
                    AVG(memory_usage_percent) AS memory_usage_percent,
                    AVG(swap_used_bytes) AS swap_used_bytes,
                    AVG(swap_total_bytes) AS swap_total_bytes,
                    AVG(swap_usage_percent) AS swap_usage_percent,
                    AVG(load_1) AS load_1,
                    AVG(load_5) AS load_5,
                    AVG(load_15) AS load_15
                FROM system_metric_samples
                WHERE sampled_at >= ? AND sampled_at <= ?
                GROUP BY bucket ORDER BY bucket
                """,
                parameters,
            ).fetchall()
            gpu_rows = self.connection.execute(
                """
                SELECT
                    gpu_uuid,
                    CAST((sampled_at - ?) / ? AS INTEGER) AS bucket,
                    AVG(sampled_at) AS sampled_at,
                    MAX(gpu_index) AS gpu_index,
                    MAX(name) AS name,
                    AVG(utilization_percent) AS utilization_percent,
                    AVG(memory_used_bytes) AS memory_used_bytes,
                    AVG(memory_total_bytes) AS memory_total_bytes,
                    MAX(temperature_celsius) AS temperature_celsius,
                    AVG(fan_percent) AS fan_percent,
                    AVG(power_watts) AS power_watts,
                    AVG(power_limit_watts) AS power_limit_watts
                FROM gpu_metric_samples
                WHERE sampled_at >= ? AND sampled_at <= ?
                GROUP BY gpu_uuid, bucket ORDER BY gpu_uuid, bucket
                """,
                parameters,
            ).fetchall()
            disk_rows = self.connection.execute(
                """
                WITH latest AS (
                    SELECT
                        mountpoint,
                        CAST((sampled_at - ?) / ? AS INTEGER) AS bucket,
                        MAX(sampled_at) AS sampled_at
                    FROM disk_metric_samples
                    WHERE sampled_at >= ? AND sampled_at <= ?
                    GROUP BY mountpoint, bucket
                )
                SELECT d.*
                FROM latest l
                JOIN disk_metric_samples d
                  ON d.mountpoint = l.mountpoint
                 AND d.sampled_at = l.sampled_at
                ORDER BY d.mountpoint, d.sampled_at
                """,
                parameters,
            ).fetchall()

        gpus: dict[str, dict[str, Any]] = {}
        for row in gpu_rows:
            item = dict(row)
            uuid = str(item.pop("gpu_uuid"))
            item.pop("bucket")
            device = gpus.setdefault(
                uuid,
                {
                    "uuid": uuid,
                    "index": item["gpu_index"],
                    "name": item["name"],
                    "points": [],
                },
            )
            item.pop("gpu_index")
            item.pop("name")
            device["points"].append(item)

        disks: dict[str, dict[str, Any]] = {}
        for row in disk_rows:
            item = dict(row)
            mountpoint = str(item.pop("mountpoint"))
            device = disks.setdefault(
                mountpoint,
                {
                    "mountpoint": mountpoint,
                    "device": item["device"],
                    "filesystem": item["filesystem"],
                    "points": [],
                },
            )
            item.pop("device")
            item.pop("filesystem")
            device["points"].append(item)

        system = []
        for row in system_rows:
            item = dict(row)
            item.pop("bucket")
            system.append(item)
        return {
            "from": since,
            "to": until,
            "bucket_seconds": bucket_seconds,
            "system": system,
            "gpus": list(gpus.values()),
            "disks": list(disks.values()),
        }

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
            self.connection.execute(
                "DELETE FROM system_metric_samples WHERE sampled_at < ?",
                (cutoff,),
            )
            self.connection.execute(
                "DELETE FROM gpu_metric_samples WHERE sampled_at < ?",
                (cutoff,),
            )
            self.connection.execute(
                "DELETE FROM disk_metric_samples WHERE sampled_at < ?",
                (cutoff,),
            )
