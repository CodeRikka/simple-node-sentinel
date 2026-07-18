from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CollectionConfig:
    interval_seconds: float = 2.0
    disk_interval_seconds: float = 10.0


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = "/var/lib/simple-node-sentinel/simple-node-sentinel.db"
    retention_days: int = 3
    cleanup_interval_seconds: int = 3600


@dataclass(frozen=True)
class FanControlConfig:
    enabled: bool = True
    minimum_percent: int = 60
    maximum_percent: int = 90
    step_percent: int = 5
    idle_temperature_celsius: float = 60.0
    idle_duration_seconds: float = 20.0
    emergency_temperature_celsius: float = 83.0
    emergency_fan_percent: int = 80


@dataclass(frozen=True)
class AlertConfig:
    high_temperature_celsius: float = 85.0
    high_duration_seconds: float = 300.0
    recovery_temperature_celsius: float = 80.0
    recovery_duration_seconds: float = 300.0
    reminder_interval_seconds: float = 7200.0


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    use_starttls: bool = True
    username: str = ""
    password_file: str = ""
    from_address: str = ""
    admin_emails: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserConfig:
    email: str | None = None


@dataclass(frozen=True)
class ProcessEndNotificationConfig:
    users: tuple[str, ...] = ()
    missing_duration_seconds: float = 20.0
    min_runtime_seconds: float = 300.0


@dataclass(frozen=True)
class Config:
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    fan_control: FanControlConfig = field(default_factory=FanControlConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    users: dict[str, UserConfig] = field(default_factory=dict)
    process_end_notifications: ProcessEndNotificationConfig = field(
        default_factory=ProcessEndNotificationConfig
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive(value: float | int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    root = _mapping(raw, "configuration")

    collection = CollectionConfig(**_mapping(root.get("collection"), "collection"))
    database = DatabaseConfig(**_mapping(root.get("database"), "database"))
    fan_control = FanControlConfig(
        **_mapping(root.get("fan_control"), "fan_control")
    )
    alerts = AlertConfig(**_mapping(root.get("alerts"), "alerts"))
    email_raw = _mapping(root.get("email"), "email").copy()
    email_raw["admin_emails"] = tuple(email_raw.get("admin_emails") or ())
    email = EmailConfig(**email_raw)

    users = {
        str(name): UserConfig(**_mapping(settings, f"users.{name}"))
        for name, settings in _mapping(root.get("users"), "users").items()
    }
    process_end_raw = _mapping(
        root.get("process_end_notifications"), "process_end_notifications"
    ).copy()
    process_end_raw["users"] = tuple(process_end_raw.get("users") or ())
    process_end_notifications = ProcessEndNotificationConfig(**process_end_raw)
    config = Config(
        collection=collection,
        database=database,
        fan_control=fan_control,
        alerts=alerts,
        email=email,
        users=users,
        process_end_notifications=process_end_notifications,
    )
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    database_path = Path(config.database.path)
    if not database_path.is_absolute() and config.database.path != ":memory:":
        raise ValueError("database.path must be absolute or :memory: for tests")
    _positive(config.collection.interval_seconds, "collection.interval_seconds")
    _positive(config.collection.disk_interval_seconds, "collection.disk_interval_seconds")
    _positive(config.database.retention_days, "database.retention_days")
    _positive(
        config.database.cleanup_interval_seconds,
        "database.cleanup_interval_seconds",
    )
    if not 0 <= config.fan_control.minimum_percent <= 100:
        raise ValueError("fan_control.minimum_percent must be between 0 and 100")
    if not 0 <= config.fan_control.maximum_percent <= 100:
        raise ValueError("fan_control.maximum_percent must be between 0 and 100")
    if config.fan_control.minimum_percent >= config.fan_control.maximum_percent:
        raise ValueError(
            "fan_control.minimum_percent must be below maximum_percent"
        )
    _positive(config.fan_control.step_percent, "fan_control.step_percent")
    if (
        config.fan_control.maximum_percent - config.fan_control.minimum_percent
    ) % config.fan_control.step_percent:
        raise ValueError("fan control range must be divisible by step_percent")
    _positive(
        config.fan_control.idle_temperature_celsius,
        "fan_control.idle_temperature_celsius",
    )
    _positive(
        config.fan_control.idle_duration_seconds,
        "fan_control.idle_duration_seconds",
    )
    if (
        config.fan_control.emergency_temperature_celsius
        <= config.fan_control.idle_temperature_celsius
    ):
        raise ValueError(
            "fan_control.emergency_temperature_celsius must exceed "
            "idle_temperature_celsius"
        )
    emergency_fan = config.fan_control.emergency_fan_percent
    if (
        emergency_fan < config.fan_control.minimum_percent
        or emergency_fan > config.fan_control.maximum_percent
        or (
            emergency_fan - config.fan_control.minimum_percent
        ) % config.fan_control.step_percent
    ):
        raise ValueError(
            "fan_control.emergency_fan_percent must be an allowed manual step"
        )
    _positive(config.alerts.high_duration_seconds, "alerts.high_duration_seconds")
    _positive(config.alerts.recovery_duration_seconds, "alerts.recovery_duration_seconds")
    _positive(
        config.alerts.reminder_interval_seconds,
        "alerts.reminder_interval_seconds",
    )
    _positive(
        config.process_end_notifications.missing_duration_seconds,
        "process_end_notifications.missing_duration_seconds",
    )
    _positive(
        config.process_end_notifications.min_runtime_seconds,
        "process_end_notifications.min_runtime_seconds",
    )
    if (
        config.alerts.recovery_temperature_celsius
        >= config.alerts.high_temperature_celsius
    ):
        raise ValueError("recovery temperature must be below high temperature")
    if config.email.enabled:
        required = {
            "smtp_host": config.email.smtp_host,
            "from_address": config.email.from_address,
            "password_file": config.email.password_file,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "email is enabled but required fields are missing: "
                + ", ".join(missing)
            )
        if not Path(config.email.password_file).is_absolute():
            raise ValueError("email.password_file must be absolute")
