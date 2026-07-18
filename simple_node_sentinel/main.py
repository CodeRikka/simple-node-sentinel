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
from pydantic import BaseModel

from .alert_manager import AlertManager
from .config import Config, load_config
from .database import Database
from .email_sender import EmailSender
from .gpu_fan_controller import FanControlError, GpuFanController
from .gpu_monitor import GpuMonitor
from .process_end_manager import ProcessEndManager
from .process_monitor import ProcessMonitor
from .system_monitor import collect_disks, collect_system_summary

LOGGER = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).with_name("web")


class FanControlRequest(BaseModel):
    mode: str
    target_percent: int | None = None
    expected_revision: int


class SentinelService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config.database.path)
        self.gpu_monitor = GpuMonitor()
        self.fan_controller = GpuFanController()
        self.process_monitor = ProcessMonitor()
        self.email_sender = EmailSender(config.email, config.users)
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
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.database.open()
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
            }
        elif not self.fan_controller.initialized:
            runtime = {
                "supported": False,
                "error": self.fan_controller.last_error or "Fan control is unavailable",
                "idle_locked": False,
                "idle_since": None,
            }
        else:
            supported, error = self.fan_controller.supports(gpu_uuid)
            runtime = {
                "supported": supported,
                "error": error,
                "idle_locked": False,
                "idle_since": None,
            }
        self._fan_runtime[gpu_uuid] = runtime
        return runtime

    def _fan_payload(
        self, gpu_uuid: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        runtime = self._runtime_for_gpu(gpu_uuid)
        idle_locked = bool(runtime["idle_locked"])
        return {
            "enabled": self.config.fan_control.enabled,
            "supported": bool(runtime["supported"]),
            "mode": state["mode"],
            "target_percent": state["target_percent"],
            "revision": state["revision"],
            "manual_allowed": bool(runtime["supported"]) and not idle_locked,
            "idle_locked": idle_locked,
            "idle_pending": bool(runtime.get("idle_pending")),
            "idle_remaining_seconds": runtime.get("idle_remaining_seconds"),
            "emergency_active": bool(runtime.get("emergency_active")),
            "error": runtime.get("error"),
            "minimum_percent": self.config.fan_control.minimum_percent,
            "maximum_percent": self.config.fan_control.maximum_percent,
            "step_percent": self.config.fan_control.step_percent,
            "idle_temperature_celsius": (
                self.config.fan_control.idle_temperature_celsius
            ),
            "idle_duration_seconds": self.config.fan_control.idle_duration_seconds,
            "emergency_temperature_celsius": (
                self.config.fan_control.emergency_temperature_celsius
            ),
            "emergency_fan_percent": self.config.fan_control.emergency_fan_percent,
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
                existing = self.database.get_fan_control_state(gpu_uuid)
                state = existing or self.database.ensure_fan_control_state(gpu_uuid)
                runtime = self._runtime_for_gpu(gpu_uuid)
                control_succeeded = True
                temperature = gpu.get("temperature_celsius")
                temperature = float(temperature) if temperature is not None else None
                process_count = int(gpu.get("process_count") or 0)
                was_idle_locked = bool(runtime["idle_locked"])
                idle_condition = (
                    process_count == 0
                    and temperature is not None
                    and temperature < self.config.fan_control.idle_temperature_celsius
                )
                if idle_condition:
                    idle_since = runtime.get("idle_since")
                    if idle_since is None:
                        idle_since = now
                    runtime["idle_since"] = idle_since
                    idle_elapsed = max(0.0, now - float(idle_since))
                    idle_locked = (
                        was_idle_locked
                        or idle_elapsed >= self.config.fan_control.idle_duration_seconds
                    )
                    runtime["idle_pending"] = not idle_locked
                    runtime["idle_remaining_seconds"] = max(
                        0.0,
                        self.config.fan_control.idle_duration_seconds - idle_elapsed,
                    )
                else:
                    runtime["idle_since"] = None
                    runtime["idle_pending"] = False
                    runtime["idle_remaining_seconds"] = None
                    if (
                        process_count > 0
                        or (
                            temperature is not None
                            and temperature
                            > self.config.fan_control.idle_temperature_celsius
                        )
                    ):
                        idle_locked = False
                    else:
                        idle_locked = was_idle_locked
                runtime["idle_locked"] = idle_locked

                emergency_applied = False
                fan_percent = gpu.get("fan_percent")
                fan_percent = (
                    float(fan_percent) if fan_percent is not None else None
                )
                emergency_target = self.config.fan_control.emergency_fan_percent
                if state["mode"] == "manual" and state["target_percent"] is not None:
                    emergency_target = max(
                        emergency_target, int(state["target_percent"])
                    )
                fan_below_emergency = (
                    fan_percent < self.config.fan_control.emergency_fan_percent
                    if fan_percent is not None
                    else (
                        state["mode"] == "manual"
                        and (
                            state["target_percent"] is None
                            or int(state["target_percent"])
                            < self.config.fan_control.emergency_fan_percent
                        )
                    )
                )
                emergency_needed = (
                    temperature is not None
                    and temperature
                    > self.config.fan_control.emergency_temperature_celsius
                    and (state["mode"] != "manual" or fan_below_emergency)
                )
                runtime["emergency_active"] = bool(
                    temperature is not None
                    and temperature
                    > self.config.fan_control.emergency_temperature_celsius
                )
                if runtime["supported"] and emergency_needed:
                    try:
                        self.fan_controller.set_manual(gpu_uuid, emergency_target)
                        if (
                            state["mode"] != "manual"
                            or state["target_percent"] != emergency_target
                        ):
                            updated = self.database.update_fan_control_state(
                                gpu_uuid,
                                "manual",
                                emergency_target,
                                int(state["revision"]),
                            )
                            if updated is not None:
                                state = updated
                        runtime["error"] = None
                        emergency_applied = True
                    except FanControlError as exc:
                        control_succeeded = False
                        runtime["error"] = str(exc)
                        LOGGER.error("Unable to apply emergency fan speed: %s", exc)

                if (
                    runtime["supported"]
                    and idle_locked
                    and (
                        state["mode"] == "manual"
                        or not was_idle_locked
                        or runtime.get("error") is not None
                    )
                ):
                    try:
                        self.fan_controller.set_auto(gpu_uuid)
                        if state["mode"] == "manual":
                            updated = self.database.update_fan_control_state(
                                gpu_uuid, "auto", None, int(state["revision"])
                            )
                            if updated is not None:
                                state = updated
                        runtime["error"] = None
                    except FanControlError as exc:
                        control_succeeded = False
                        runtime["error"] = str(exc)
                        LOGGER.error("Unable to restore automatic fan control: %s", exc)

                if (
                    runtime["supported"]
                    and gpu_uuid not in self._fan_restored
                    and existing is not None
                    and not idle_locked
                    and not emergency_applied
                ):
                    try:
                        if state["mode"] == "manual":
                            self.fan_controller.set_manual(
                                gpu_uuid, int(state["target_percent"])
                            )
                        else:
                            self.fan_controller.set_auto(gpu_uuid)
                        runtime["error"] = None
                    except (FanControlError, TypeError, ValueError) as exc:
                        control_succeeded = False
                        runtime["error"] = str(exc)
                        LOGGER.error("Unable to restore fan state for %s: %s", gpu_uuid, exc)
                if control_succeeded:
                    self._fan_restored.add(gpu_uuid)
                gpu["fan_control"] = self._fan_payload(gpu_uuid, state)

    def set_fan_control(
        self,
        gpu_uuid: str,
        mode: str,
        target_percent: int | None,
        expected_revision: int,
    ) -> dict[str, Any]:
        if mode not in {"auto", "manual"}:
            raise HTTPException(status_code=422, detail="mode must be auto or manual")
        config = self.config.fan_control
        if mode == "manual":
            if target_percent is None:
                raise HTTPException(
                    status_code=422,
                    detail="target_percent is required in manual mode",
                )
            if (
                target_percent < config.minimum_percent
                or target_percent > config.maximum_percent
                or (target_percent - config.minimum_percent) % config.step_percent
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"target_percent must be {config.minimum_percent}-"
                        f"{config.maximum_percent} in {config.step_percent}% steps"
                    ),
                )
        else:
            target_percent = None

        with self._fan_control_lock:
            state = self.database.get_fan_control_state(gpu_uuid)
            if state is None:
                raise HTTPException(status_code=404, detail="GPU not found")
            runtime = self._runtime_for_gpu(gpu_uuid)
            current = self._fan_payload(gpu_uuid, state)
            if int(state["revision"]) != expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "Fan state changed", "fan_control": current},
                )
            if not runtime["supported"]:
                raise HTTPException(
                    status_code=503,
                    detail=runtime.get("error") or "Fan control is unavailable",
                )
            if mode == "manual" and runtime["idle_locked"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "Manual control is locked while the GPU is idle "
                            "and below the temperature threshold"
                        ),
                        "fan_control": current,
                    },
                )
            try:
                if mode == "manual":
                    self.fan_controller.set_manual(gpu_uuid, int(target_percent))
                else:
                    self.fan_controller.set_auto(gpu_uuid)
            except FanControlError as exc:
                runtime["error"] = str(exc)
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            updated = self.database.update_fan_control_state(
                gpu_uuid, mode, target_percent, expected_revision
            )
            if updated is None:
                latest = self.database.get_fan_control_state(gpu_uuid)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Fan state changed",
                        "fan_control": (
                            self._fan_payload(gpu_uuid, latest)
                            if latest is not None
                            else None
                        ),
                    },
                )
            runtime["error"] = None
            payload = self._fan_payload(gpu_uuid, updated)
            with self._snapshot_lock:
                for gpu in self._snapshot["gpus"]:
                    if gpu.get("uuid") == gpu_uuid:
                        gpu["fan_control"] = copy.deepcopy(payload)
                        break
            return payload

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

    @application.put("/api/gpus/{gpu_uuid}/fan-control")
    def set_gpu_fan_control(
        gpu_uuid: str, request: FanControlRequest
    ) -> dict[str, Any]:
        return service.set_fan_control(
            gpu_uuid,
            request.mode,
            request.target_percent,
            request.expected_revision,
        )

    @application.get("/api/gpu-processes")
    def gpu_processes() -> list[dict[str, Any]]:
        return service.snapshot("gpu_processes")

    @application.get("/api/users")
    def users() -> list[dict[str, Any]]:
        return service.snapshot("users")

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
