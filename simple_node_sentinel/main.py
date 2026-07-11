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
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alert_manager import AlertManager
from .config import Config, load_config
from .database import Database
from .email_sender import EmailSender
from .gpu_monitor import GpuMonitor
from .process_end_manager import ProcessEndManager
from .process_monitor import ProcessMonitor
from .system_monitor import collect_disks, collect_system_summary

LOGGER = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).with_name("web")


class SentinelService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config.database.path)
        self.gpu_monitor = GpuMonitor()
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
