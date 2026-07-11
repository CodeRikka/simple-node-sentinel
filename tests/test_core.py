from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

from simple_node_sentinel.alert_manager import AlertManager
from simple_node_sentinel.config import (
    AlertConfig,
    Config,
    DatabaseConfig,
    EmailConfig,
    ProcessEndNotificationConfig,
    UserConfig,
    load_config,
)
from simple_node_sentinel.database import Database
from simple_node_sentinel.email_sender import EmailSender
from simple_node_sentinel.main import create_app
from simple_node_sentinel.process_end_manager import ProcessEndManager
from simple_node_sentinel.process_monitor import ProcessMonitor, sanitize_command
from simple_node_sentinel.system_monitor import collect_disks


class CommandSanitizationTests(unittest.TestCase):
    def test_sanitizes_separate_and_equals_values(self) -> None:
        command = sanitize_command(
            [
                "python",
                "train.py",
                "--token",
                "abc",
                "--api-key=def",
                "--epochs",
                "3",
            ]
        )
        self.assertEqual(
            command,
            "python train.py --token ******** --api-key=******** --epochs 3",
        )
        self.assertNotIn("abc", command)
        self.assertNotIn("def", command)

    @patch("simple_node_sentinel.process_monitor.psutil.Process")
    def test_process_disappearing_is_skipped(self, process_class) -> None:
        process_class.side_effect = psutil.NoSuchProcess(42)
        result = ProcessMonitor().inspect_gpu_process(
            {"pid": 42, "gpu_index": 0, "gpu_uuid": "GPU-1"}
        )
        self.assertIsNone(result)

    def test_cpu_sample_is_reused_within_collection_cycle(self) -> None:
        monitor = ProcessMonitor()
        process = Mock()
        process.cpu_percent.return_value = 12.5
        key = (42, 100.0)
        self.assertEqual(monitor._cpu_percent(process, key), 12.5)
        self.assertEqual(monitor._cpu_percent(process, key), 12.5)
        process.cpu_percent.assert_called_once_with(interval=None)


class ConfigTests(unittest.TestCase):
    def test_loads_valid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "database:\n"
                f"  path: {directory}/sentinel.db\n"
                "email:\n"
                "  enabled: false\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.database.retention_days, 3)
            self.assertFalse(config.email.enabled)

    def test_rejects_relative_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("database:\n  path: relative.db\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "absolute"):
                load_config(path)


class DatabaseTests(unittest.TestCase):
    def test_process_lifecycle_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "test.db"))
            database.open()
            try:
                process = {
                    "gpu_uuid": "GPU-1",
                    "gpu_index": 0,
                    "pid": 123,
                    "started_at": 10.0,
                    "username": "alice",
                    "command": "python train.py",
                }
                database.reconcile_gpu_processes([process], observed_at=100.0)
                database.reconcile_gpu_processes([], observed_at=200.0)
                database.cleanup(retention_days=3, now=200.0 + 4 * 86400)
                count = database.connection.execute(
                    "SELECT COUNT(*) FROM gpu_process_records"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                database.close()
            self.assertFalse((Path(directory) / "test.db-wal").exists())

    def test_active_records_are_not_cleaned(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            database.reconcile_gpu_processes(
                [
                    {
                        "gpu_uuid": "GPU-1",
                        "gpu_index": 0,
                        "pid": 123,
                        "started_at": 10.0,
                    }
                ],
                observed_at=1.0,
            )
            database.cleanup(retention_days=1, now=10 * 86400)
            count = database.connection.execute(
                "SELECT COUNT(*) FROM gpu_process_records"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            database.close()


class AlertTests(unittest.TestCase):
    def test_alert_reminder_and_recovery_state_machine(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            alerts = AlertManager(
                AlertConfig(
                    high_temperature_celsius=85,
                    high_duration_seconds=5,
                    recovery_temperature_celsius=80,
                    recovery_duration_seconds=5,
                    reminder_interval_seconds=10,
                ),
                database,
                EmailSender(
                    EmailConfig(enabled=False, admin_emails=("admin@example.com",)),
                    {"alice": UserConfig(email="alice@example.com")},
                ),
            )
            gpu = {"uuid": "GPU-1", "index": 0, "temperature_celsius": 90}
            process = {"gpu_uuid": "GPU-1", "username": "alice"}
            alerts.evaluate([gpu], [process], monotonic_now=0, wall_now=100)
            alerts.evaluate([gpu], [process], monotonic_now=5, wall_now=105)
            alerts.evaluate([gpu], [process], monotonic_now=15, wall_now=115)
            gpu["temperature_celsius"] = 75
            alerts.evaluate([gpu], [], monotonic_now=16, wall_now=116)
            alerts.evaluate([gpu], [], monotonic_now=21, wall_now=121)

            row = database.list_alerts()[0]
            self.assertEqual(row["status"], "recovered")
            self.assertEqual(row["users"], ["alice"])
            statuses = [
                value[0]
                for value in database.connection.execute(
                    "SELECT status FROM email_records ORDER BY id"
                ).fetchall()
            ]
            self.assertEqual(statuses, ["disabled", "disabled"])
            state = alerts.states["GPU-1"]
            self.assertIsNone(state.alert_id)
            self.assertEqual(state.last_notification, 15)
        finally:
            database.close()

    def test_no_reminder_below_high_temperature(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            alerts = AlertManager(
                AlertConfig(high_duration_seconds=1, reminder_interval_seconds=2),
                database,
                EmailSender(EmailConfig(enabled=False), {}),
            )
            gpu = {"uuid": "GPU-1", "index": 0, "temperature_celsius": 90}
            alerts.evaluate([gpu], [], monotonic_now=0, wall_now=100)
            alerts.evaluate([gpu], [], monotonic_now=1, wall_now=101)
            gpu["temperature_celsius"] = 82
            alerts.evaluate([gpu], [], monotonic_now=10, wall_now=110)
            count = database.connection.execute(
                "SELECT COUNT(*) FROM email_records"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            database.close()


class ProcessEndNotificationTests(unittest.TestCase):
    def test_selected_user_is_notified_after_twenty_seconds(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            sender = EmailSender(
                EmailConfig(
                    enabled=False,
                    admin_emails=("admin@example.com",),
                ),
                {"alice": UserConfig(email="alice@example.com")},
            )
            manager = ProcessEndManager(
                ProcessEndNotificationConfig(
                    users=("alice",), missing_duration_seconds=20
                ),
                sender,
                database,
            )
            process = {
                "gpu_uuid": "GPU-1",
                "gpu_index": 0,
                "pid": 123,
                "started_at": 100.0,
                "username": "alice",
                "executable": "python",
                "command": "python train.py",
            }
            manager.evaluate([process], monotonic_now=0, wall_now=100)
            manager.evaluate([], monotonic_now=1, wall_now=101)
            manager.evaluate([], monotonic_now=20, wall_now=120)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM email_records"
                ).fetchone()[0],
                0,
            )
            manager.evaluate([], monotonic_now=21, wall_now=121)
            row = database.connection.execute(
                "SELECT kind, recipients_json, status FROM email_records"
            ).fetchone()
            self.assertEqual(row["kind"], "process_end")
            self.assertEqual(row["recipients_json"], '["alice@example.com"]')
            self.assertEqual(row["status"], "disabled")
        finally:
            database.close()


class DiskAndApiTests(unittest.TestCase):
    @patch("simple_node_sentinel.system_monitor.psutil.disk_usage")
    @patch("simple_node_sentinel.system_monitor.psutil.disk_partitions")
    def test_virtual_filesystems_are_filtered(self, partitions, usage) -> None:
        partitions.return_value = [
            SimpleNamespace(
                device="/dev/sda1", mountpoint="/", fstype="ext4", opts="rw"
            ),
            SimpleNamespace(
                device="tmpfs", mountpoint="/run", fstype="tmpfs", opts="rw"
            ),
        ]
        usage.return_value = SimpleNamespace(
            total=100, used=30, free=70, percent=30.0
        )
        disks = collect_disks()
        self.assertEqual([disk["mountpoint"] for disk in disks], ["/"])

    def test_api_exposes_only_get_routes(self) -> None:
        app = create_app(Config(database=DatabaseConfig(path=":memory:")))
        monitored_paths = {
            "/api/summary",
            "/api/gpus",
            "/api/gpu-processes",
            "/api/users",
            "/api/disks",
            "/api/alerts",
            "/health",
        }
        route_methods = {
            route.path: route.methods
            for route in app.routes
            if getattr(route, "path", None) in monitored_paths
        }
        self.assertEqual(set(route_methods), monitored_paths)
        for methods in route_methods.values():
            self.assertEqual(methods, {"GET"})

    def test_application_lifecycle_uses_temporary_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                database=DatabaseConfig(path=str(Path(directory) / "app.db"))
            )
            app = create_app(config)
            service = app.state.sentinel

            async def exercise_lifespan() -> None:
                with (
                    patch.object(service.gpu_monitor, "initialize"),
                    patch.object(service.gpu_monitor, "close"),
                    patch.object(service, "collect_once"),
                ):
                    async with app.router.lifespan_context(app):
                        self.assertTrue(service.running)
                    self.assertFalse(service.running)

            asyncio.run(exercise_lifespan())
            self.assertIsNone(service.database._connection)


if __name__ == "__main__":
    unittest.main()
