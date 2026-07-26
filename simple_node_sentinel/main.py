from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alert_manager import AlertManager
from .config import Config, load_config
from .database import Database
from .email_sender import EmailSender
from .gpu_fan_controller import FanControlError, GpuFanController
from .gpu_monitor import GpuMonitor
from .process_end_manager import ProcessEndManager
from .process_monitor import ProcessMonitor
from .system_monitor import collect_disks, collect_system_summary
from .user_directory import apply_legacy_user_import, sync_user_settings

LOGGER = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).with_name("web")
USER_SYNC_INTERVAL_SECONDS = 6 * 3600


class FanCurvePointModel(BaseModel):
    temperature_celsius: float = Field(ge=0, le=120)
    fan_percent: int = Field(ge=0, le=100)


class FanProfileRequest(BaseModel):
    minimum_percent: int = Field(ge=0, le=100)
    maximum_percent: int = Field(ge=0, le=100)
    idle_temperature_celsius: float = Field(gt=0, le=120)
    idle_duration_seconds: float = Field(gt=0)
    curve_points: list[FanCurvePointModel] = Field(default_factory=list)
    expected_revision: int


class UserSettingsUpdateRequest(BaseModel):
    email: str | None = None
    is_admin: bool = False
    notify_temperature: bool = True
    notify_process_end: bool = False


class SentinelService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config.database.path)
        self.gpu_monitor = GpuMonitor()
        self.fan_controller = GpuFanController()
        self.process_monitor = ProcessMonitor()
        self.email_sender = EmailSender(config.email, self.database)
        self.alert_manager = AlertManager(
            config.alerts, self.database, self.email_sender
        )
        self.process_end_manager = ProcessEndManager(
            config.process_end_notifications,
            self.email_sender,
            self.database,
        )
        self._snapshot_lock = threading.Lock()
        self._fan_control_lock = threading.RLock()
        self._fan_runtime: dict[str, dict[str, Any]] = {}
        self._fan_restored: set[str] = set()
        self._snapshot: dict[str, Any] = {
            "summary": {},
            "gpus": [],
            "gpu_processes": [],
            "users": [],
            "disks": [],
            "sampled_at": None,
        }
        self._last_disk_sample = 0.0
        self._last_cleanup = 0.0
        self._last_user_sync = 0.0
        self._user_sync_result: dict[str, Any] | None = None
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.database.open()
        self._user_sync_result = sync_user_settings(self.database)
        apply_legacy_user_import(self.database, self.config.legacy_import)
        self._last_user_sync = time.monotonic()
        self.gpu_monitor.initialize()
        if self.config.fan_control.enabled:
            self.fan_controller.initialize()
        self.running = True
        self._task = asyncio.create_task(self._run(), name="sentinel-collector")

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.fan_controller.close()
        self.gpu_monitor.close()
        self.database.close()

    async def _run(self) -> None:
        interval = self.config.collection.interval_seconds
        while self.running:
            started = time.monotonic()
            try:
                await asyncio.to_thread(self.collect_once)
                self.last_success = time.time()
                self.last_error = None
            except Exception as exc:  # collector must survive isolated failures
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("Collection cycle failed")
            delay = max(0.0, interval - (time.monotonic() - started))
            await asyncio.sleep(delay)

    def collect_once(self) -> None:
        monotonic_now = time.monotonic()
        if (
            monotonic_now - self._last_user_sync
            >= USER_SYNC_INTERVAL_SECONDS
        ):
            self._user_sync_result = sync_user_settings(self.database)
            self._last_user_sync = monotonic_now

        gpus, raw_gpu_processes = self.gpu_monitor.collect()
        self._refresh_fan_controls(gpus, monotonic_now)
        gpu_processes = self.process_monitor.inspect_gpu_processes(
            raw_gpu_processes
        )
        summary = collect_system_summary()
        users = self.process_monitor.user_summary(gpu_processes)

        with self._snapshot_lock:
            disks = self._snapshot["disks"]
        disk_sampled = False
        if (
            monotonic_now - self._last_disk_sample
            >= self.config.collection.disk_interval_seconds
        ):
            disks = collect_disks()
            self._last_disk_sample = monotonic_now
            disk_sampled = True

        if self.gpu_monitor.initialized and self.gpu_monitor.last_error is None:
            self.database.reconcile_gpu_processes(gpu_processes)
            self.process_end_manager.evaluate(
                gpu_processes, monotonic_now=monotonic_now
            )
        self.alert_manager.evaluate(
            gpus, gpu_processes, monotonic_now=monotonic_now
        )
        if (
            monotonic_now - self._last_cleanup
            >= self.config.database.cleanup_interval_seconds
        ):
            self.database.cleanup(self.config.database.retention_days)
            self._last_cleanup = monotonic_now

        users_by_gpu: dict[str, set[str]] = {}
        for process in gpu_processes:
            users_by_gpu.setdefault(process["gpu_uuid"], set()).add(
                process["username"]
            )
        for gpu in gpus:
            gpu["users"] = sorted(users_by_gpu.get(gpu["uuid"], set()))

        sampled_at = time.time()
        snapshot = {
            "summary": summary,
            "gpus": gpus,
            "gpu_processes": gpu_processes,
            "users": users,
            "disks": disks,
            "sampled_at": sampled_at,
        }
        with self._snapshot_lock:
            self._snapshot = snapshot
        self.database.record_metric_snapshot(
            sampled_at,
            summary,
            gpus,
            disks if disk_sampled else None,
        )

    def _runtime_for_gpu(self, gpu_uuid: str) -> dict[str, Any]:
        runtime = self._fan_runtime.get(gpu_uuid)
        if runtime is not None:
            return runtime
        if not self.config.fan_control.enabled:
            runtime = {
                "supported": False,
                "error": "Fan control is disabled by configuration",
                "idle_locked": False,
                "idle_since": None,
                "applied_percent": None,
            }
        elif not self.fan_controller.initialized:
            runtime = {
                "supported": False,
                "error": self.fan_controller.last_error or "Fan control is unavailable",
                "idle_locked": False,
                "idle_since": None,
                "applied_percent": None,
            }
        else:
            supported, error = self.fan_controller.supports(gpu_uuid)
            runtime = {
                "supported": supported,
                "error": error,
                "idle_locked": False,
                "idle_since": None,
                "applied_percent": None,
            }
        self._fan_runtime[gpu_uuid] = runtime
        return runtime

    @staticmethod
    def _curve_target(
        temperature: float | None, points: list[dict[str, Any]]
    ) -> int | None:
        if temperature is None or not points:
            return None
        desired: int | None = None
        for point in points:
            if temperature >= float(point["temperature_celsius"]):
                desired = int(point["fan_percent"])
        return desired

    def _fan_payload(
        self, gpu_uuid: str, profile: dict[str, Any]
    ) -> dict[str, Any]:
        runtime = self._runtime_for_gpu(gpu_uuid)
        idle_locked = bool(runtime["idle_locked"])
        points = list(profile.get("curve_points") or [])
        mode = "curve" if points else "auto"
        return {
            "enabled": self.config.fan_control.enabled,
            "supported": bool(runtime["supported"]),
            "mode": mode,
            "applied_percent": runtime.get("applied_percent"),
            "revision": profile["revision"],
            "curve_active": mode == "curve" and not idle_locked,
            "idle_locked": idle_locked,
            "idle_pending": bool(runtime.get("idle_pending")),
            "idle_remaining_seconds": runtime.get("idle_remaining_seconds"),
            "error": runtime.get("error"),
            "minimum_percent": profile["minimum_percent"],
            "maximum_percent": profile["maximum_percent"],
            "step_percent": self.config.fan_control.step_percent,
            "idle_temperature_celsius": profile["idle_temperature_celsius"],
            "idle_duration_seconds": profile["idle_duration_seconds"],
            "curve_points": points,
        }

    def _refresh_fan_controls(
        self,
        gpus: list[dict[str, Any]],
        monotonic_now: float | None = None,
    ) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        with self._fan_control_lock:
            for gpu in gpus:
                gpu_uuid = str(gpu["uuid"])
                existing = self.database.get_fan_profile(gpu_uuid)
                profile = existing or self.database.ensure_fan_profile(gpu_uuid)
                runtime = self._runtime_for_gpu(gpu_uuid)
                control_succeeded = True
                temperature = gpu.get("temperature_celsius")
                temperature = float(temperature) if temperature is not None else None
                process_count = int(gpu.get("process_count") or 0)
                points = list(profile.get("curve_points") or [])
                idle_temperature = float(profile["idle_temperature_celsius"])
                idle_duration = float(profile["idle_duration_seconds"])
                was_idle_locked = bool(runtime["idle_locked"])

                idle_condition = (
                    process_count == 0
                    and temperature is not None
                    and temperature < idle_temperature
                )
                if idle_condition:
                    idle_since = runtime.get("idle_since")
                    if idle_since is None:
                        idle_since = now
                    runtime["idle_since"] = idle_since
                    idle_elapsed = max(0.0, now - float(idle_since))
                    idle_locked = (
                        was_idle_locked or idle_elapsed >= idle_duration
                    )
                    runtime["idle_pending"] = not idle_locked
                    runtime["idle_remaining_seconds"] = max(
                        0.0, idle_duration - idle_elapsed
                    )
                else:
                    runtime["idle_since"] = None
                    runtime["idle_pending"] = False
                    runtime["idle_remaining_seconds"] = None
                    if (
                        process_count > 0
                        or (
                            temperature is not None
                            and temperature > idle_temperature
                        )
                    ):
                        idle_locked = False
                    else:
                        idle_locked = was_idle_locked
                runtime["idle_locked"] = idle_locked

                if not points or idle_locked:
                    runtime["applied_percent"] = None
                    needs_auto = runtime["supported"] and (
                        not points
                        or not was_idle_locked
                        or runtime.get("last_written_percent") is not None
                        or gpu_uuid not in self._fan_restored
                        or runtime.get("error") is not None
                    )
                    if needs_auto:
                        try:
                            self.fan_controller.set_auto(gpu_uuid)
                            runtime["error"] = None
                            runtime["last_written_percent"] = None
                        except FanControlError as exc:
                            control_succeeded = False
                            runtime["error"] = str(exc)
                            LOGGER.error(
                                "Unable to restore automatic fan control: %s",
                                exc,
                            )
                    if control_succeeded:
                        self._fan_restored.add(gpu_uuid)
                    gpu["fan_control"] = self._fan_payload(gpu_uuid, profile)
                    continue

                desired = self._curve_target(temperature, points)
                applied = runtime.get("applied_percent")
                if desired is not None:
                    applied = max(
                        int(applied) if applied is not None else 0, desired
                    )
                if applied is not None:
                    applied = max(
                        int(profile["minimum_percent"]),
                        min(int(profile["maximum_percent"]), int(applied)),
                    )
                    runtime["applied_percent"] = applied
                    should_write = runtime["supported"] and (
                        runtime.get("last_written_percent") != applied
                        or gpu_uuid not in self._fan_restored
                    )
                    if should_write:
                        try:
                            self.fan_controller.set_manual(gpu_uuid, applied)
                            runtime["error"] = None
                            runtime["last_written_percent"] = applied
                        except FanControlError as exc:
                            control_succeeded = False
                            runtime["error"] = str(exc)
                            LOGGER.error(
                                "Unable to apply fan curve for %s: %s",
                                gpu_uuid,
                                exc,
                            )
                elif runtime["supported"] and (
                    runtime.get("last_written_percent") is not None
                    or gpu_uuid not in self._fan_restored
                ):
                    try:
                        self.fan_controller.set_auto(gpu_uuid)
                        runtime["error"] = None
                        runtime["last_written_percent"] = None
                    except FanControlError as exc:
                        control_succeeded = False
                        runtime["error"] = str(exc)
                        LOGGER.error(
                            "Unable to restore fan state for %s: %s",
                            gpu_uuid,
                            exc,
                        )

                if control_succeeded:
                    self._fan_restored.add(gpu_uuid)
                gpu["fan_control"] = self._fan_payload(gpu_uuid, profile)

    def _validate_fan_profile_request(
        self, request: FanProfileRequest
    ) -> list[dict[str, Any]]:
        step = self.config.fan_control.step_percent
        if request.minimum_percent >= request.maximum_percent:
            raise HTTPException(
                status_code=422,
                detail="minimum_percent must be below maximum_percent",
            )
        if (
            request.maximum_percent - request.minimum_percent
        ) % step:
            raise HTTPException(
                status_code=422,
                detail="fan range must be divisible by step_percent",
            )
        if request.idle_duration_seconds <= 0:
            raise HTTPException(
                status_code=422,
                detail="idle_duration_seconds must be greater than zero",
            )

        points = [
            {
                "temperature_celsius": float(point.temperature_celsius),
                "fan_percent": int(point.fan_percent),
            }
            for point in request.curve_points
        ]
        points.sort(key=lambda item: item["temperature_celsius"])
        seen_temps: set[float] = set()
        previous_fan: int | None = None
        for point in points:
            temperature = point["temperature_celsius"]
            fan_percent = point["fan_percent"]
            if temperature in seen_temps:
                raise HTTPException(
                    status_code=422,
                    detail="curve temperatures must be unique",
                )
            seen_temps.add(temperature)
            if (
                fan_percent < request.minimum_percent
                or fan_percent > request.maximum_percent
                or (fan_percent - request.minimum_percent) % step
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"fan_percent must be {request.minimum_percent}-"
                        f"{request.maximum_percent} in {step}% steps"
                    ),
                )
            if previous_fan is not None and fan_percent < previous_fan:
                raise HTTPException(
                    status_code=422,
                    detail="curve fan percents must be non-decreasing with temperature",
                )
            previous_fan = fan_percent
        return points

    def set_fan_profile(
        self, gpu_uuid: str, request: FanProfileRequest
    ) -> dict[str, Any]:
        points = self._validate_fan_profile_request(request)
        with self._fan_control_lock:
            profile = self.database.get_fan_profile(gpu_uuid)
            if profile is None:
                raise HTTPException(status_code=404, detail="GPU not found")
            current = self._fan_payload(gpu_uuid, profile)
            if int(profile["revision"]) != request.expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Fan profile changed",
                        "fan_control": current,
                    },
                )
            updated = self.database.update_fan_profile(
                gpu_uuid,
                minimum_percent=request.minimum_percent,
                maximum_percent=request.maximum_percent,
                idle_temperature_celsius=request.idle_temperature_celsius,
                idle_duration_seconds=request.idle_duration_seconds,
                curve_points=points,
                expected_revision=request.expected_revision,
            )
            if updated is None:
                latest = self.database.get_fan_profile(gpu_uuid)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Fan profile changed",
                        "fan_control": (
                            self._fan_payload(gpu_uuid, latest)
                            if latest is not None
                            else None
                        ),
                    },
                )

            runtime = self._runtime_for_gpu(gpu_uuid)
            runtime["applied_percent"] = None
            runtime["last_written_percent"] = None
            if not points and runtime["supported"]:
                try:
                    self.fan_controller.set_auto(gpu_uuid)
                    runtime["error"] = None
                except FanControlError as exc:
                    runtime["error"] = str(exc)
                    raise HTTPException(status_code=503, detail=str(exc)) from exc

            payload = self._fan_payload(gpu_uuid, updated)
            with self._snapshot_lock:
                for gpu in self._snapshot["gpus"]:
                    if gpu.get("uuid") == gpu_uuid:
                        gpu["fan_control"] = copy.deepcopy(payload)
                        break
            return payload

    def list_user_settings(self) -> dict[str, Any]:
        return {
            "users": self.database.list_user_settings(active_only=True),
            "last_sync": self._user_sync_result,
        }

    def sync_users(self) -> dict[str, Any]:
        self._user_sync_result = sync_user_settings(self.database)
        self._last_user_sync = time.monotonic()
        return {
            "users": self.database.list_user_settings(active_only=True),
            "last_sync": self._user_sync_result,
        }

    def update_user_settings(
        self, username: str, request: UserSettingsUpdateRequest
    ) -> dict[str, Any]:
        updated = self.database.update_user_setting(
            username,
            email=request.email,
            is_admin=request.is_admin,
            notify_temperature=request.notify_temperature,
            notify_process_end=request.notify_process_end,
        )
        if updated is None:
            raise HTTPException(
                status_code=404,
                detail="User not found or inactive on this system",
            )
        return updated

    def snapshot(self, key: str) -> Any:
        with self._snapshot_lock:
            return copy.deepcopy(self._snapshot[key])

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.running and self.last_error is None else "degraded",
            "collector_running": self.running,
            "last_successful_sample": self.last_success,
            "last_error": self.last_error,
            "nvml_available": self.gpu_monitor.initialized,
            "nvml_error": self.gpu_monitor.last_error,
            "fan_control_enabled": self.config.fan_control.enabled,
            "fan_control_available": self.fan_controller.initialized,
            "fan_control_error": (
                self.fan_controller.last_error
                if self.config.fan_control.enabled
                else "Fan control is disabled by configuration"
            ),
        }


def create_app(config: Config) -> FastAPI:
    service = SentinelService(config)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    application = FastAPI(
        title="Simple Node Sentinel",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.sentinel = service
    application.mount("/static", StaticFiles(directory=WEB_DIRECTORY), name="static")

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIRECTORY / "index.html")

    @application.get("/api/summary")
    def summary() -> dict[str, Any]:
        return {
            "sampled_at": service.snapshot("sampled_at"),
            **service.snapshot("summary"),
        }

    @application.get("/api/gpus")
    def gpus() -> list[dict[str, Any]]:
        return service.snapshot("gpus")

    @application.put("/api/gpus/{gpu_uuid}/fan-profile")
    def set_gpu_fan_profile(
        gpu_uuid: str, request: FanProfileRequest
    ) -> dict[str, Any]:
        return service.set_fan_profile(gpu_uuid, request)

    @application.get("/api/gpu-processes")
    def gpu_processes() -> list[dict[str, Any]]:
        return service.snapshot("gpu_processes")

    @application.get("/api/users")
    def users() -> list[dict[str, Any]]:
        return service.snapshot("users")

    @application.get("/api/settings/users")
    def settings_users() -> dict[str, Any]:
        return service.list_user_settings()

    @application.post("/api/settings/users/sync")
    def settings_users_sync() -> dict[str, Any]:
        return service.sync_users()

    @application.patch("/api/settings/users/{username}")
    def settings_users_update(
        username: str, request: UserSettingsUpdateRequest
    ) -> dict[str, Any]:
        return service.update_user_settings(username, request)

    @application.get("/api/disks")
    def disks() -> list[dict[str, Any]]:
        return service.snapshot("disks")

    @application.get("/api/alerts")
    def alerts() -> list[dict[str, Any]]:
        return service.alert_manager.overlay_live_values(
            service.database.list_alerts()
        )

    @application.get("/api/history")
    def history(
        range_seconds: int = Query(default=3600, ge=60, le=259200),
        max_points: int = Query(default=720, ge=60, le=1000),
    ) -> dict[str, Any]:
        until = time.time()
        since = until - range_seconds
        return service.database.query_metric_history(since, until, max_points)

    @application.get("/health")
    def health() -> dict[str, Any]:
        return service.health()

    return application


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple Node Sentinel")
    parser.add_argument("--config", required=True, help="Path to YAML configuration")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    uvicorn.run(create_app(config), host="127.0.0.1", port=8080)


if __name__ == "__main__":
    run()
