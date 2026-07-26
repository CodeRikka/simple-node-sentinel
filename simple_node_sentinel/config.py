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
    step_percent: int = 5


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


@dataclass(frozen=True)
class ProcessEndNotificationConfig:
    missing_duration_seconds: float = 20.0
    min_runtime_seconds: float = 300.0


@dataclass(frozen=True)
class LegacyImport:
    """One-time values read from older YAML layouts."""

    users: dict[str, str] = field(default_factory=dict)
    admin_emails: tuple[str, ...] = ()
    process_end_users: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    fan_control: FanControlConfig = field(default_factory=FanControlConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    process_end_notifications: ProcessEndNotificationConfig = field(
        default_factory=ProcessEndNotificationConfig
    )
    legacy_import: LegacyImport | None = None


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive(value: float | int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _extract_legacy(root: dict[str, Any]) -> LegacyImport | None:
    users_raw = _mapping(root.get("users"), "users")
    users = {
        str(name): str(settings.get("email") or "").strip()
        for name, settings in users_raw.items()
        if isinstance(settings, dict) and settings.get("email")
    }
    email_raw = _mapping(root.get("email"), "email")
    admin_emails = tuple(
        str(item).strip()
        for item in (email_raw.get("admin_emails") or ())
        if str(item).strip()
    )
    process_end_raw = _mapping(
        root.get("process_end_notifications"), "process_end_notifications"
    )
    process_end_users = tuple(
        str(item).strip()
        for item in (process_end_raw.get("users") or ())
        if str(item).strip()
    )
    if not users and not admin_emails and not process_end_users:
        return None
    return LegacyImport(
        users=users,
        admin_emails=admin_emails,
        process_end_users=process_end_users,
    )


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    root = _mapping(raw, "configuration")

    collection = CollectionConfig(**_mapping(root.get("collection"), "collection"))
    database = DatabaseConfig(**_mapping(root.get("database"), "database"))

    fan_raw = _mapping(root.get("fan_control"), "fan_control")
    fan_control = FanControlConfig(
        enabled=bool(fan_raw.get("enabled", True)),
        step_percent=int(fan_raw.get("step_percent", 5)),
    )
    alerts = AlertConfig(**_mapping(root.get("alerts"), "alerts"))

    email_raw = {
        key: value
        for key, value in _mapping(root.get("email"), "email").items()
        if key != "admin_emails"
    }
    email = EmailConfig(**email_raw)

    process_end_raw = {
        key: value
        for key, value in _mapping(
            root.get("process_end_notifications"), "process_end_notifications"
        ).items()
        if key != "users"
    }
    process_end_notifications = ProcessEndNotificationConfig(**process_end_raw)

    config = Config(
        collection=collection,
        database=database,
        fan_control=fan_control,
        alerts=alerts,
        email=email,
        process_end_notifications=process_end_notifications,
        legacy_import=_extract_legacy(root),
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
    _positive(config.fan_control.step_percent, "fan_control.step_percent")
    if config.fan_control.step_percent > 100:
        raise ValueError("fan_control.step_percent must be between 1 and 100")
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
