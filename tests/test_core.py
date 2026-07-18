from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil
from fastapi import HTTPException

from simple_node_sentinel.alert_manager import AlertManager
from simple_node_sentinel.config import (
    AlertConfig,
    Config,
    DatabaseConfig,
    EmailConfig,
    FanControlConfig,
    ProcessEndNotificationConfig,
    UserConfig,
    load_config,
)
from simple_node_sentinel.database import Database
from simple_node_sentinel.email_sender import EmailSender
from simple_node_sentinel.gpu_fan_controller import (
    FanControlError,
    GpuFanController,
)
from simple_node_sentinel.main import SentinelService, create_app
from simple_node_sentinel.process_end_manager import ProcessEndManager
from simple_node_sentinel.process_monitor import (
    ProcessMonitor,
    is_primary_user,
    login_uid_min,
    sanitize_command,
)
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

    def test_primary_users_include_root_and_login_uids(self) -> None:
        self.assertTrue(is_primary_user("root", 0))
        self.assertTrue(is_primary_user("alice", login_uid_min()))
        self.assertFalse(is_primary_user("daemon", 1))
        self.assertFalse(is_primary_user("nobody", 65534))


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


class FanControllerTests(unittest.TestCase):
    def test_manual_and_auto_apply_to_every_fan(self) -> None:
        controller = GpuFanController()
        controller.initialized = True
        controller.library = Mock()
        controller.library.nvmlDeviceSetFanSpeed_v2.return_value = 0
        controller.library.nvmlDeviceSetDefaultFanSpeed_v2.return_value = 0
        handle = object()
        with (
            patch.object(controller, "_handle_by_uuid", return_value=handle),
            patch.object(controller, "_fan_count", return_value=2),
        ):
            controller.set_manual("GPU-1", 75)
            controller.set_auto("GPU-1")
        self.assertEqual(
            controller.library.nvmlDeviceSetFanSpeed_v2.call_args_list,
            [unittest.mock.call(handle, 0, 75), unittest.mock.call(handle, 1, 75)],
        )
        self.assertEqual(
            controller.library.nvmlDeviceSetDefaultFanSpeed_v2.call_args_list,
            [unittest.mock.call(handle, 0), unittest.mock.call(handle, 1)],
        )

    def test_partial_manual_failure_attempts_automatic_rollback(self) -> None:
        controller = GpuFanController()
        controller.initialized = True
        controller.library = Mock()
        controller.library.nvmlDeviceSetFanSpeed_v2.side_effect = [0, 1]
        controller.library.nvmlDeviceSetDefaultFanSpeed_v2.return_value = 0
        controller.library.nvmlErrorString.return_value = b"Not supported"
        with (
            patch.object(controller, "_handle_by_uuid", return_value=object()),
            patch.object(controller, "_fan_count", return_value=2),
            self.assertRaises(FanControlError),
        ):
            controller.set_manual("GPU-1", 80)
        self.assertEqual(
            controller.library.nvmlDeviceSetDefaultFanSpeed_v2.call_count, 2
        )


class FanControlServiceTests(unittest.TestCase):
    def make_service(self) -> SentinelService:
        service = SentinelService(
            Config(
                database=DatabaseConfig(path=":memory:"),
                fan_control=FanControlConfig(),
            )
        )
        service.database.open()
        service.fan_controller.initialized = True
        service.fan_controller.supports = Mock(return_value=(True, None))
        service.fan_controller.set_auto = Mock()
        service.fan_controller.set_manual = Mock()
        return service

    def test_idle_low_temperature_forces_auto_and_unlocks_on_process(self) -> None:
        service = self.make_service()
        try:
            state = service.database.ensure_fan_control_state("GPU-1")
            service.database.update_fan_control_state(
                "GPU-1", "manual", 80, state["revision"]
            )
            gpu = {
                "uuid": "GPU-1",
                "process_count": 0,
                "temperature_celsius": 55,
            }
            service._refresh_fan_controls([gpu], monotonic_now=100)
            state = service.database.get_fan_control_state("GPU-1")
            self.assertEqual(state["mode"], "manual")
            self.assertTrue(gpu["fan_control"]["idle_pending"])
            self.assertFalse(gpu["fan_control"]["idle_locked"])
            service._refresh_fan_controls([gpu], monotonic_now=119)
            self.assertEqual(
                service.database.get_fan_control_state("GPU-1")["mode"],
                "manual",
            )
            service._refresh_fan_controls([gpu], monotonic_now=120)
            state = service.database.get_fan_control_state("GPU-1")
            self.assertEqual(state["mode"], "auto")
            self.assertTrue(gpu["fan_control"]["idle_locked"])
            self.assertFalse(gpu["fan_control"]["manual_allowed"])
            service.fan_controller.set_auto.assert_called_once_with("GPU-1")

            gpu["process_count"] = 1
            service._refresh_fan_controls([gpu], monotonic_now=121)
            self.assertFalse(gpu["fan_control"]["idle_locked"])
            self.assertTrue(gpu["fan_control"]["manual_allowed"])
            self.assertEqual(gpu["fan_control"]["mode"], "auto")
        finally:
            service.database.close()

    def test_high_temperature_enforces_eighty_percent_manual_fan(self) -> None:
        service = self.make_service()
        try:
            service.database.ensure_fan_control_state("GPU-1")
            gpu = {
                "uuid": "GPU-1",
                "process_count": 1,
                "temperature_celsius": 84,
                "fan_percent": 70,
            }
            service._refresh_fan_controls([gpu], monotonic_now=100)
            state = service.database.get_fan_control_state("GPU-1")
            self.assertEqual(state["mode"], "manual")
            self.assertEqual(state["target_percent"], 80)
            self.assertEqual(state["revision"], 1)
            service.fan_controller.set_manual.assert_called_once_with("GPU-1", 80)
        finally:
            service.database.close()

    def test_emergency_policy_does_not_reduce_higher_manual_target(self) -> None:
        service = self.make_service()
        try:
            state = service.database.ensure_fan_control_state("GPU-1")
            service.database.update_fan_control_state(
                "GPU-1", "manual", 90, state["revision"]
            )
            gpu = {
                "uuid": "GPU-1",
                "process_count": 1,
                "temperature_celsius": 84,
                "fan_percent": 70,
            }
            service._refresh_fan_controls([gpu], monotonic_now=100)
            state = service.database.get_fan_control_state("GPU-1")
            self.assertEqual(state["target_percent"], 90)
            self.assertEqual(state["revision"], 1)
            service.fan_controller.set_manual.assert_called_once_with("GPU-1", 90)
        finally:
            service.database.close()

    def test_unknown_temperature_does_not_start_idle_lock(self) -> None:
        service = self.make_service()
        try:
            gpu = {
                "uuid": "GPU-1",
                "process_count": 0,
                "temperature_celsius": None,
            }
            service._refresh_fan_controls([gpu])
            self.assertFalse(gpu["fan_control"]["idle_locked"])
            service.fan_controller.set_auto.assert_not_called()
        finally:
            service.database.close()

    def test_persisted_manual_state_is_restored_by_uuid(self) -> None:
        service = self.make_service()
        try:
            state = service.database.ensure_fan_control_state("GPU-stable")
            service.database.update_fan_control_state(
                "GPU-stable", "manual", 85, state["revision"]
            )
            service._refresh_fan_controls(
                [
                    {
                        "uuid": "GPU-stable",
                        "index": 7,
                        "process_count": 0,
                        "temperature_celsius": 70,
                    }
                ]
            )
            service.fan_controller.set_manual.assert_called_once_with(
                "GPU-stable", 85
            )
        finally:
            service.database.close()

    def test_stale_revision_is_rejected_without_second_hardware_write(self) -> None:
        service = self.make_service()
        try:
            service.database.ensure_fan_control_state("GPU-1")
            service._fan_runtime["GPU-1"] = {
                "supported": True,
                "error": None,
                "idle_locked": False,
            }
            first = service.set_fan_control("GPU-1", "manual", 75, 0)
            self.assertEqual(first["revision"], 1)
            with self.assertRaises(HTTPException) as raised:
                service.set_fan_control("GPU-1", "manual", 80, 0)
            self.assertEqual(raised.exception.status_code, 409)
            service.fan_controller.set_manual.assert_called_once_with("GPU-1", 75)
        finally:
            service.database.close()

    def test_failed_hardware_write_does_not_change_persisted_state(self) -> None:
        service = self.make_service()
        try:
            service.database.ensure_fan_control_state("GPU-1")
            service._fan_runtime["GPU-1"] = {
                "supported": True,
                "error": None,
                "idle_locked": False,
            }
            service.fan_controller.set_manual.side_effect = FanControlError("failed")
            with self.assertRaises(HTTPException) as raised:
                service.set_fan_control("GPU-1", "manual", 75, 0)
            self.assertEqual(raised.exception.status_code, 503)
            state = service.database.get_fan_control_state("GPU-1")
            self.assertEqual(state["mode"], "auto")
            self.assertEqual(state["revision"], 0)
        finally:
            service.database.close()


class DatabaseTests(unittest.TestCase):
    def test_fan_control_state_uses_revision_and_survives_cleanup(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            initial = database.ensure_fan_control_state("GPU-1")
            updated = database.update_fan_control_state(
                "GPU-1", "manual", 75, initial["revision"], updated_at=100
            )
            self.assertEqual(updated["revision"], 1)
            self.assertIsNone(
                database.update_fan_control_state("GPU-1", "manual", 80, 0)
            )
            database.cleanup(retention_days=1, now=10 * 86400)
            persisted = database.get_fan_control_state("GPU-1")
            self.assertEqual(persisted["mode"], "manual")
            self.assertEqual(persisted["target_percent"], 75)
        finally:
            database.close()

    def test_metric_history_is_grouped_and_preserves_temperature_peaks(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            for sampled_at, usage, temperature in (
                (100.0, 10.0, 70.0),
                (102.0, 30.0, 90.0),
            ):
                database.record_metric_snapshot(
                    sampled_at,
                    {
                        "cpu": {
                            "usage_percent": usage,
                            "load_average": {"1m": 1, "5m": 2, "15m": 3},
                        },
                        "cpu_temperature": {"max_celsius": temperature},
                        "memory": {
                            "used_bytes": 50,
                            "total_bytes": 100,
                            "usage_percent": 50,
                        },
                        "swap": {
                            "used_bytes": None,
                            "total_bytes": None,
                            "usage_percent": None,
                        },
                    },
                    [
                        {
                            "uuid": "GPU-1",
                            "index": 0,
                            "name": "Test GPU",
                            "utilization_percent": usage,
                            "memory_used_bytes": 25,
                            "memory_total_bytes": 100,
                            "temperature_celsius": temperature,
                            "fan_percent": None,
                            "power_watts": 100,
                            "power_limit_watts": 200,
                        },
                        {
                            "uuid": "GPU-2",
                            "index": 1,
                            "name": "Second GPU",
                            "utilization_percent": None,
                        },
                    ],
                    [
                        {
                            "mountpoint": "/",
                            "device": "/dev/sda1",
                            "filesystem": "ext4",
                            "used_bytes": 40 + usage,
                            "total_bytes": 100,
                            "available_bytes": 60 - usage,
                            "usage_percent": 40 + usage,
                        }
                    ],
                )

            history = database.query_metric_history(100, 103, max_points=1)
            self.assertEqual(len(history["system"]), 1)
            self.assertEqual(history["system"][0]["cpu_usage_percent"], 20)
            self.assertEqual(history["system"][0]["cpu_temperature_celsius"], 90)
            self.assertIsNone(history["system"][0]["swap_usage_percent"])
            self.assertEqual(
                {gpu["uuid"] for gpu in history["gpus"]},
                {"GPU-1", "GPU-2"},
            )
            self.assertEqual(
                next(
                    gpu for gpu in history["gpus"] if gpu["uuid"] == "GPU-1"
                )["points"][0]["temperature_celsius"],
                90,
            )
            self.assertEqual(history["disks"][0]["points"][0]["sampled_at"], 102)
            self.assertEqual(history["disks"][0]["points"][0]["usage_percent"], 70)
        finally:
            database.close()

    def test_metric_samples_follow_retention(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            summary = {
                "cpu": {},
                "cpu_temperature": {},
                "memory": {},
                "swap": {},
            }
            database.record_metric_snapshot(100, summary, [], [])
            database.record_metric_snapshot(400000, summary, [], [])
            database.cleanup(retention_days=3, now=400001)
            rows = database.connection.execute(
                "SELECT sampled_at FROM system_metric_samples ORDER BY sampled_at"
            ).fetchall()
            self.assertEqual([row["sampled_at"] for row in rows], [400000])
        finally:
            database.close()

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
    def test_restart_scan_recovers_persisted_active_alert(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            gpu = {"uuid": "GPU-1", "index": 0, "temperature_celsius": 90}
            alert_id = database.create_alert(
                gpu, {"alice"}, temperature=90, triggered_at=100
            )
            manager = AlertManager(
                AlertConfig(),
                database,
                EmailSender(EmailConfig(enabled=False), {}),
            )
            recovered_gpu = {
                "uuid": "GPU-1",
                "index": 0,
                "temperature_celsius": 70,
            }
            manager.evaluate(
                [recovered_gpu], [], monotonic_now=10, wall_now=200
            )
            alert = next(
                item for item in database.list_alerts() if item["id"] == alert_id
            )
            self.assertEqual(alert["status"], "recovered")
            self.assertEqual(alert["recovered_at"], 200)
            self.assertEqual(alert["current_temperature"], 70)
        finally:
            database.close()

    def test_recovered_alert_cannot_be_changed_back_to_active(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            gpu = {"uuid": "GPU-1", "index": 0}
            alert_id = database.create_alert(
                gpu, set(), temperature=90, triggered_at=100
            )
            database.update_alert(
                alert_id, set(), 70, 90, recovered_at=200
            )
            database.update_alert(alert_id, {"alice"}, 95, 95)
            alert = next(
                item for item in database.list_alerts() if item["id"] == alert_id
            )
            self.assertEqual(alert["status"], "recovered")
            self.assertEqual(alert["recovered_at"], 200)
            self.assertEqual(alert["current_temperature"], 70)
        finally:
            database.close()

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
                    users=("alice",),
                    missing_duration_seconds=20,
                    min_runtime_seconds=300,
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
            manager.evaluate([process], monotonic_now=0, wall_now=400)
            manager.evaluate([], monotonic_now=1, wall_now=401)
            manager.evaluate([], monotonic_now=20, wall_now=420)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM email_records"
                ).fetchone()[0],
                0,
            )
            manager.evaluate([], monotonic_now=21, wall_now=421)
            row = database.connection.execute(
                "SELECT kind, recipients_json, status FROM email_records"
            ).fetchone()
            self.assertEqual(row["kind"], "process_end")
            self.assertEqual(row["recipients_json"], '["alice@example.com"]')
            self.assertEqual(row["status"], "disabled")
        finally:
            database.close()

    def test_short_lived_process_is_not_notified(self) -> None:
        database = Database(":memory:")
        database.open()
        try:
            sender = EmailSender(
                EmailConfig(enabled=False),
                {"alice": UserConfig(email="alice@example.com")},
            )
            manager = ProcessEndManager(
                ProcessEndNotificationConfig(
                    users=("alice",),
                    missing_duration_seconds=20,
                    min_runtime_seconds=300,
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
            manager.evaluate([], monotonic_now=21, wall_now=121)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM email_records"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(manager.tracked, {})
        finally:
            database.close()


class DiskAndApiTests(unittest.TestCase):
    @patch("simple_node_sentinel.system_monitor.physical_disk_info")
    @patch("simple_node_sentinel.system_monitor.psutil.disk_usage")
    @patch("simple_node_sentinel.system_monitor.psutil.disk_partitions")
    def test_virtual_filesystems_are_filtered(
        self, partitions, usage, physical_info
    ) -> None:
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
        physical_info.return_value = [
            {
                "name": "sda",
                "device": "/dev/sda",
                "model": "Test disk",
                "size_bytes": 1000,
                "rotational": False,
            }
        ]
        disks = collect_disks()
        self.assertEqual([disk["mountpoint"] for disk in disks], ["/"])
        self.assertEqual(disks[0]["physical_disks"][0]["device"], "/dev/sda")
        physical_info.assert_called_once_with("/dev/sda1")

    def test_api_exposes_monitoring_and_fan_control_routes(self) -> None:
        app = create_app(Config(database=DatabaseConfig(path=":memory:")))
        monitored_paths = {
            "/api/summary",
            "/api/gpus",
            "/api/gpu-processes",
            "/api/users",
            "/api/disks",
            "/api/alerts",
            "/api/history",
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
        fan_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None)
            == "/api/gpus/{gpu_uuid}/fan-control"
        )
        self.assertEqual(fan_route.methods, {"PUT"})

        history_route = next(
            route for route in app.routes if getattr(route, "path", None) == "/api/history"
        )
        query_parameters = {
            parameter.name: parameter.field_info
            for parameter in history_route.dependant.query_params
        }
        constraints = {
            name: {
                type(item).__name__: getattr(
                    item, type(item).__name__.lower()
                )
                for item in field_info.metadata
            }
            for name, field_info in query_parameters.items()
        }
        self.assertEqual(constraints["range_seconds"], {"Ge": 60, "Le": 259200})
        self.assertEqual(constraints["max_points"], {"Ge": 60, "Le": 1000})

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
