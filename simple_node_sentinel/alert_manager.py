from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import AlertConfig
from .database import Database
from .email_sender import EmailSender


@dataclass
class GpuAlertState:
    high_since: float | None = None
    recovery_since: float | None = None
    alert_id: int | None = None
    users: set[str] = field(default_factory=set)
    max_temperature: float = 0.0
    current_temperature: float | None = None
    last_notification: float | None = None


class AlertManager:
    def __init__(
        self,
        config: AlertConfig,
        database: Database,
        email_sender: EmailSender,
    ) -> None:
        self.config = config
        self.database = database
        self.email_sender = email_sender
        self.states: dict[str, GpuAlertState] = {}

    def _notify(
        self,
        state: GpuAlertState,
        gpu: dict[str, Any],
        temperature: float,
        kind: str,
    ) -> None:
        label = f"GPU {gpu['index']} ({gpu['uuid']})"
        if kind == "recovery":
            subject = f"[Simple Node Sentinel] {label} temperature recovered"
            body = (
                f"{label} recovered to {temperature:.1f}°C. "
                f"Maximum observed temperature was {state.max_temperature:.1f}°C."
            )
        else:
            subject = f"[Simple Node Sentinel] High temperature on {label}"
            body = (
                f"{label} is at {temperature:.1f}°C. "
                f"Maximum observed temperature is {state.max_temperature:.1f}°C."
            )
        status, recipients, error = self.email_sender.send(
            subject, body, state.users
        )
        self.database.record_email(
            state.alert_id, kind, recipients, status, error
        )

    def evaluate(
        self,
        gpus: list[dict[str, Any]],
        processes: list[dict[str, Any]],
        monotonic_now: float | None = None,
        wall_now: float | None = None,
    ) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        wall = wall_now if wall_now is not None else time.time()
        users_by_gpu: dict[str, set[str]] = {}
        for process in processes:
            username = process.get("username")
            if username:
                users_by_gpu.setdefault(process["gpu_uuid"], set()).add(username)

        observed_gpus: set[str] = set()
        for gpu in gpus:
            uuid = gpu["uuid"]
            observed_gpus.add(uuid)
            temperature = gpu.get("temperature_celsius")
            state = self.states.setdefault(uuid, GpuAlertState())
            if temperature is None:
                state.high_since = None
                state.recovery_since = None
                state.current_temperature = None
                continue
            temperature = float(temperature)
            state.current_temperature = temperature
            if state.alert_id is None:
                if temperature > self.config.high_temperature_celsius:
                    if state.high_since is None:
                        state.high_since = now
                        state.users.clear()
                        state.max_temperature = temperature
                    state.users.update(users_by_gpu.get(uuid, set()))
                    state.max_temperature = max(state.max_temperature, temperature)
                    if now - state.high_since >= self.config.high_duration_seconds:
                        state.alert_id = self.database.create_alert(
                            gpu, state.users, temperature, wall
                        )
                        if (
                            state.last_notification is None
                            or now - state.last_notification
                            >= self.config.reminder_interval_seconds
                        ):
                            self._notify(state, gpu, temperature, "alert")
                            state.last_notification = now
                else:
                    state.high_since = None
                    state.users.clear()
                    state.max_temperature = 0.0
                    state.current_temperature = None
                continue

            state.users.update(users_by_gpu.get(uuid, set()))
            state.max_temperature = max(state.max_temperature, temperature)
            if temperature < self.config.recovery_temperature_celsius:
                if state.recovery_since is None:
                    state.recovery_since = now
                if (
                    now - state.recovery_since
                    >= self.config.recovery_duration_seconds
                ):
                    self.database.update_alert(
                        state.alert_id,
                        state.users,
                        temperature,
                        state.max_temperature,
                        recovered_at=wall,
                    )
                    self.states[uuid] = GpuAlertState(
                        last_notification=state.last_notification
                    )
                continue
            state.recovery_since = None
            if (
                temperature > self.config.high_temperature_celsius
                and state.last_notification is not None
                and now - state.last_notification
                >= self.config.reminder_interval_seconds
            ):
                self.database.update_alert(
                    state.alert_id,
                    state.users,
                    temperature,
                    state.max_temperature,
                )
                self._notify(state, gpu, temperature, "reminder")
                state.last_notification = now

        for uuid, state in self.states.items():
            if uuid not in observed_gpus:
                state.recovery_since = None
                if state.alert_id is None:
                    state.high_since = None
                    state.users.clear()
                    state.max_temperature = 0.0
                    state.current_temperature = None

    def overlay_live_values(
        self, alerts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        active = {
            state.alert_id: state
            for state in self.states.values()
            if state.alert_id is not None
        }
        for alert in alerts:
            state = active.get(alert["id"])
            if state is not None:
                alert["current_temperature"] = state.current_temperature
                alert["max_temperature"] = state.max_temperature
                alert["users"] = sorted(state.users)
        return alerts
