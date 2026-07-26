from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_FAN_MINIMUM_PERCENT = 30
DEFAULT_FAN_MAXIMUM_PERCENT = 100
DEFAULT_FAN_IDLE_TEMPERATURE_CELSIUS = 60.0
DEFAULT_FAN_IDLE_DURATION_SECONDS = 20.0
DEFAULT_FAN_CURVE_POINTS: tuple[tuple[float, int], ...] = ((80.0, 80),)


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
            self._connection.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS user_settings (
                    username TEXT PRIMARY KEY,
                    uid INTEGER,
                    email TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    notify_temperature INTEGER NOT NULL DEFAULT 1,
                    notify_process_end INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    last_synced_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gpu_fan_profiles (
                    gpu_uuid TEXT PRIMARY KEY,
                    minimum_percent INTEGER NOT NULL,
                    maximum_percent INTEGER NOT NULL,
                    idle_temperature_celsius REAL NOT NULL,
                    idle_duration_seconds REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gpu_fan_curve_points (
                    id INTEGER PRIMARY KEY,
                    gpu_uuid TEXT NOT NULL,
                    temperature_celsius REAL NOT NULL,
                    fan_percent INTEGER NOT NULL,
                    UNIQUE(gpu_uuid, temperature_celsius),
                    FOREIGN KEY(gpu_uuid) REFERENCES gpu_fan_profiles(gpu_uuid)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_process_ended
                    ON gpu_process_records(ended_at);
                CREATE INDEX IF NOT EXISTS idx_alert_recovered
                    ON alerts(recovered_at);
                CREATE INDEX IF NOT EXISTS idx_email_created
                    ON email_records(created_at);
                CREATE INDEX IF NOT EXISTS idx_fan_curve_gpu
                    ON gpu_fan_curve_points(gpu_uuid, temperature_celsius);
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
            self._migrate_legacy_fan_state()
            self._connection.commit()

    def _migrate_legacy_fan_state(self) -> None:
        assert self._connection is not None
        exists = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='gpu_fan_control_state'
            """
        ).fetchone()
        if exists is None:
            return
        now = time.time()
        rows = self._connection.execute(
            "SELECT gpu_uuid FROM gpu_fan_control_state"
        ).fetchall()
        for row in rows:
            gpu_uuid = str(row["gpu_uuid"])
            self._connection.execute(
                """
                INSERT OR IGNORE INTO gpu_fan_profiles (
                    gpu_uuid, minimum_percent, maximum_percent,
                    idle_temperature_celsius, idle_duration_seconds,
                    revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    gpu_uuid,
                    DEFAULT_FAN_MINIMUM_PERCENT,
                    DEFAULT_FAN_MAXIMUM_PERCENT,
                    DEFAULT_FAN_IDLE_TEMPERATURE_CELSIUS,
                    DEFAULT_FAN_IDLE_DURATION_SECONDS,
                    now,
                ),
            )
            point_count = self._connection.execute(
                "SELECT COUNT(*) FROM gpu_fan_curve_points WHERE gpu_uuid=?",
                (gpu_uuid,),
            ).fetchone()[0]
            if point_count == 0:
                for temperature, fan_percent in DEFAULT_FAN_CURVE_POINTS:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO gpu_fan_curve_points (
                            gpu_uuid, temperature_celsius, fan_percent
                        ) VALUES (?, ?, ?)
                        """,
                        (gpu_uuid, temperature, fan_percent),
                    )
        self._connection.execute("DROP TABLE gpu_fan_control_state")

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

    @staticmethod
    def _bool(value: Any) -> bool:
        return bool(int(value))

    def _profile_from_rows(
        self, profile_row: sqlite3.Row, point_rows: Iterable[sqlite3.Row]
    ) -> dict[str, Any]:
        points = [
            {
                "temperature_celsius": float(row["temperature_celsius"]),
                "fan_percent": int(row["fan_percent"]),
            }
            for row in point_rows
        ]
        points.sort(key=lambda item: item["temperature_celsius"])
        mode = "curve" if points else "auto"
        return {
            "gpu_uuid": str(profile_row["gpu_uuid"]),
            "minimum_percent": int(profile_row["minimum_percent"]),
            "maximum_percent": int(profile_row["maximum_percent"]),
            "idle_temperature_celsius": float(
                profile_row["idle_temperature_celsius"]
            ),
            "idle_duration_seconds": float(profile_row["idle_duration_seconds"]),
            "revision": int(profile_row["revision"]),
            "updated_at": float(profile_row["updated_at"]),
            "mode": mode,
            "curve_points": points,
        }

    def _load_fan_profile_locked(self, gpu_uuid: str) -> dict[str, Any] | None:
        profile = self.connection.execute(
            "SELECT * FROM gpu_fan_profiles WHERE gpu_uuid=?",
            (gpu_uuid,),
        ).fetchone()
        if profile is None:
            return None
        points = self.connection.execute(
            """
            SELECT temperature_celsius, fan_percent
            FROM gpu_fan_curve_points
            WHERE gpu_uuid=?
            ORDER BY temperature_celsius
            """,
            (gpu_uuid,),
        ).fetchall()
        return self._profile_from_rows(profile, points)

    def ensure_fan_profile(self, gpu_uuid: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self.connection:
            existing = self._load_fan_profile_locked(gpu_uuid)
            if existing is not None:
                return existing
            self.connection.execute(
                """
                INSERT INTO gpu_fan_profiles (
                    gpu_uuid, minimum_percent, maximum_percent,
                    idle_temperature_celsius, idle_duration_seconds,
                    revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    gpu_uuid,
                    DEFAULT_FAN_MINIMUM_PERCENT,
                    DEFAULT_FAN_MAXIMUM_PERCENT,
                    DEFAULT_FAN_IDLE_TEMPERATURE_CELSIUS,
                    DEFAULT_FAN_IDLE_DURATION_SECONDS,
                    now,
                ),
            )
            for temperature, fan_percent in DEFAULT_FAN_CURVE_POINTS:
                self.connection.execute(
                    """
                    INSERT INTO gpu_fan_curve_points (
                        gpu_uuid, temperature_celsius, fan_percent
                    ) VALUES (?, ?, ?)
                    """,
                    (gpu_uuid, temperature, fan_percent),
                )
            created = self._load_fan_profile_locked(gpu_uuid)
        if created is None:
            raise RuntimeError("unable to create GPU fan profile")
        return created

    def get_fan_profile(self, gpu_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load_fan_profile_locked(gpu_uuid)

    def update_fan_profile(
        self,
        gpu_uuid: str,
        *,
        minimum_percent: int,
        maximum_percent: int,
        idle_temperature_celsius: float,
        idle_duration_seconds: float,
        curve_points: Iterable[dict[str, Any]],
        expected_revision: int,
        updated_at: float | None = None,
    ) -> dict[str, Any] | None:
        now = updated_at if updated_at is not None else time.time()
        points = [
            {
                "temperature_celsius": float(point["temperature_celsius"]),
                "fan_percent": int(point["fan_percent"]),
            }
            for point in curve_points
        ]
        points.sort(key=lambda item: item["temperature_celsius"])
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE gpu_fan_profiles
                SET minimum_percent=?, maximum_percent=?,
                    idle_temperature_celsius=?, idle_duration_seconds=?,
                    revision=revision + 1, updated_at=?
                WHERE gpu_uuid=? AND revision=?
                """,
                (
                    minimum_percent,
                    maximum_percent,
                    idle_temperature_celsius,
                    idle_duration_seconds,
                    now,
                    gpu_uuid,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self.connection.execute(
                "DELETE FROM gpu_fan_curve_points WHERE gpu_uuid=?",
                (gpu_uuid,),
            )
            self.connection.executemany(
                """
                INSERT INTO gpu_fan_curve_points (
                    gpu_uuid, temperature_celsius, fan_percent
                ) VALUES (?, ?, ?)
                """,
                [
                    (gpu_uuid, point["temperature_celsius"], point["fan_percent"])
                    for point in points
                ],
            )
            return self._load_fan_profile_locked(gpu_uuid)

    def _user_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "username": str(row["username"]),
            "uid": row["uid"],
            "email": row["email"],
            "is_admin": self._bool(row["is_admin"]),
            "notify_temperature": self._bool(row["notify_temperature"]),
            "notify_process_end": self._bool(row["notify_process_end"]),
            "active": self._bool(row["active"]),
            "updated_at": float(row["updated_at"]),
            "last_synced_at": float(row["last_synced_at"]),
        }

    def list_user_settings(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM user_settings"
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY username"
        with self._lock:
            rows = self.connection.execute(query).fetchall()
        return [self._user_from_row(row) for row in rows]

    def get_user_setting(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM user_settings WHERE username=?",
                (username,),
            ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def sync_user_settings(
        self, system_users: Iterable[tuple[str, int]]
    ) -> dict[str, Any]:
        now = time.time()
        present = {username: uid for username, uid in system_users}
        with self._lock, self.connection:
            existing = {
                str(row["username"]): dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM user_settings"
                ).fetchall()
            }
            created = 0
            reactivated = 0
            deactivated = 0
            for username, uid in present.items():
                row = existing.get(username)
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO user_settings (
                            username, uid, email, is_admin, notify_temperature,
                            notify_process_end, active, updated_at, last_synced_at
                        ) VALUES (?, ?, NULL, 0, 1, 0, 1, ?, ?)
                        """,
                        (username, uid, now, now),
                    )
                    created += 1
                    continue
                was_active = self._bool(row["active"])
                self.connection.execute(
                    """
                    UPDATE user_settings
                    SET uid=?, active=1, last_synced_at=?
                    WHERE username=?
                    """,
                    (uid, now, username),
                )
                if not was_active:
                    reactivated += 1
            for username, row in existing.items():
                if username in present:
                    continue
                if not self._bool(row["active"]):
                    self.connection.execute(
                        "UPDATE user_settings SET last_synced_at=? WHERE username=?",
                        (now, username),
                    )
                    continue
                self.connection.execute(
                    """
                    UPDATE user_settings
                    SET active=0, last_synced_at=?, updated_at=?
                    WHERE username=?
                    """,
                    (now, now, username),
                )
                deactivated += 1
        return {
            "created": created,
            "reactivated": reactivated,
            "deactivated": deactivated,
            "active": len(present),
            "synced_at": now,
        }

    def update_user_setting(
        self,
        username: str,
        *,
        email: str | None,
        is_admin: bool,
        notify_temperature: bool,
        notify_process_end: bool,
    ) -> dict[str, Any] | None:
        now = time.time()
        normalized_email = (email or "").strip() or None
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE user_settings
                SET email=?, is_admin=?, notify_temperature=?,
                    notify_process_end=?, updated_at=?
                WHERE username=? AND active=1
                """,
                (
                    normalized_email,
                    int(is_admin),
                    int(notify_temperature),
                    int(notify_process_end),
                    now,
                    username,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = self.connection.execute(
                "SELECT * FROM user_settings WHERE username=?",
                (username,),
            ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def apply_legacy_user_import(
        self,
        users: dict[str, str],
        admin_emails: Iterable[str],
        process_end_users: Iterable[str],
    ) -> None:
        admin_set = {email.lower() for email in admin_emails}
        process_end_set = set(process_end_users)
        now = time.time()
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT * FROM user_settings WHERE active=1"
            ).fetchall()
            for row in rows:
                username = str(row["username"])
                email = row["email"]
                if username in users and not email:
                    email = users[username]
                is_admin = self._bool(row["is_admin"])
                if email and str(email).lower() in admin_set:
                    is_admin = True
                notify_process_end = self._bool(row["notify_process_end"])
                if username in process_end_set:
                    notify_process_end = True
                if (
                    email == row["email"]
                    and is_admin == self._bool(row["is_admin"])
                    and notify_process_end == self._bool(row["notify_process_end"])
                ):
                    continue
                self.connection.execute(
                    """
                    UPDATE user_settings
                    SET email=?, is_admin=?, notify_process_end=?, updated_at=?
                    WHERE username=?
                    """,
                    (
                        email,
                        int(is_admin),
                        int(notify_process_end),
                        now,
                        username,
                    ),
                )

    def admin_emails(self) -> list[str]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT email FROM user_settings
                WHERE active=1 AND is_admin=1
                  AND email IS NOT NULL AND TRIM(email) != ''
                ORDER BY email
                """
            ).fetchall()
        return [str(row["email"]) for row in rows]

    def process_end_usernames(self) -> set[str]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT username FROM user_settings
                WHERE active=1 AND notify_process_end=1
                """
            ).fetchall()
        return {str(row["username"]) for row in rows}

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
