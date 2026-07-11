from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import ProcessEndNotificationConfig
from .database import Database
from .email_sender import EmailSender

ProcessKey = tuple[str, int, float]


@dataclass
class TrackedGpuProcess:
    details: dict[str, Any]
    missing_since: float | None = None


class ProcessEndManager:
    def __init__(
        self,
        config: ProcessEndNotificationConfig,
        email_sender: EmailSender,
        database: Database,
    ) -> None:
        self.config = config
        self.email_sender = email_sender
        self.database = database
        self.tracked: dict[ProcessKey, TrackedGpuProcess] = {}
        self.notified: dict[ProcessKey, float] = {}

    @staticmethod
    def _key(process: dict[str, Any]) -> ProcessKey:
        return (
            str(process["gpu_uuid"]),
            int(process["pid"]),
            float(process["started_at"]),
        )

    def evaluate(
        self,
        processes: list[dict[str, Any]],
        monotonic_now: float | None = None,
        wall_now: float | None = None,
    ) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        wall = wall_now if wall_now is not None else time.time()
        configured_users = set(self.config.users)
        current: set[ProcessKey] = set()

        for process in processes:
            if process.get("username") not in configured_users:
                continue
            key = self._key(process)
            current.add(key)
            if key in self.notified:
                continue
            tracked = self.tracked.get(key)
            if tracked is None:
                self.tracked[key] = TrackedGpuProcess(dict(process))
            else:
                tracked.details = dict(process)
                tracked.missing_since = None

        for key, tracked in list(self.tracked.items()):
            if key in current:
                continue
            if tracked.missing_since is None:
                tracked.missing_since = now
                continue
            if now - tracked.missing_since < self.config.missing_duration_seconds:
                continue
            runtime = wall - float(tracked.details["started_at"])
            if runtime < self.config.min_runtime_seconds:
                del self.tracked[key]
                continue
            self._notify(tracked.details, wall)
            self.notified[key] = now
            del self.tracked[key]

        cutoff = now - 3 * 86400
        self.notified = {
            key: notified_at
            for key, notified_at in self.notified.items()
            if notified_at >= cutoff
        }

    def _notify(self, process: dict[str, Any], ended_at: float) -> None:
        username = process["username"]
        subject = (
            f"[Simple Node Sentinel] GPU process {process['pid']} has ended"
        )
        body = (
            f"Your process on GPU {process['gpu_index']} has ended.\n\n"
            f"PID: {process['pid']}\n"
            f"GPU UUID: {process['gpu_uuid']}\n"
            f"Executable: {process.get('executable') or 'N/A'}\n"
            f"Command: {process.get('command') or 'N/A'}\n"
            f"Started at: {process['started_at']:.3f}\n"
            f"Detected ended at: {ended_at:.3f}"
        )
        status, recipients, error = self.email_sender.send(
            subject,
            body,
            [username],
            include_admins=False,
        )
        self.database.record_email(
            None, "process_end", recipients, status, error
        )
