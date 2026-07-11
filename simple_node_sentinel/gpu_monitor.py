from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

import pynvml

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def _text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


class GpuMonitor:
    def __init__(self) -> None:
        self.initialized = False
        self.last_error: str | None = None

    def initialize(self) -> None:
        try:
            pynvml.nvmlInit()
            self.initialized = True
            self.last_error = None
        except pynvml.NVMLError as exc:
            self.last_error = str(exc)
            LOGGER.warning("NVML is unavailable: %s", exc)

    def close(self) -> None:
        if self.initialized:
            try:
                pynvml.nvmlShutdown()
            except pynvml.NVMLError as exc:
                LOGGER.warning("Unable to shut down NVML cleanly: %s", exc)
            finally:
                self.initialized = False

    def _optional(self, function: Callable[..., T], *args: Any) -> T | None:
        try:
            return function(*args)
        except pynvml.NVMLError:
            return None

    def _running_processes(self, handle: Any) -> list[Any]:
        for name in (
            "nvmlDeviceGetComputeRunningProcesses_v3",
            "nvmlDeviceGetComputeRunningProcesses_v2",
            "nvmlDeviceGetComputeRunningProcesses",
        ):
            function = getattr(pynvml, name, None)
            if function is not None:
                try:
                    return list(function(handle))
                except pynvml.NVMLError:
                    continue
        return []

    def collect(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.initialized:
            return [], []
        gpus: list[dict[str, Any]] = []
        processes: list[dict[str, Any]] = []
        try:
            count = pynvml.nvmlDeviceGetCount()
        except pynvml.NVMLError as exc:
            self.last_error = str(exc)
            return [], []

        for index in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                uuid = _text(pynvml.nvmlDeviceGetUUID(handle))
                name = _text(pynvml.nvmlDeviceGetName(handle))
                memory = self._optional(pynvml.nvmlDeviceGetMemoryInfo, handle)
                utilization = self._optional(
                    pynvml.nvmlDeviceGetUtilizationRates, handle
                )
                temperature = self._optional(
                    pynvml.nvmlDeviceGetTemperature,
                    handle,
                    pynvml.NVML_TEMPERATURE_GPU,
                )
                fan = self._optional(pynvml.nvmlDeviceGetFanSpeed, handle)
                power_mw = self._optional(
                    pynvml.nvmlDeviceGetPowerUsage, handle
                )
                power_limit_mw = self._optional(
                    pynvml.nvmlDeviceGetEnforcedPowerLimit, handle
                )
                gpu_processes = self._running_processes(handle)
                for process in gpu_processes:
                    used_memory = getattr(process, "usedGpuMemory", None)
                    if used_memory == getattr(
                        pynvml, "NVML_VALUE_NOT_AVAILABLE", object()
                    ):
                        used_memory = None
                    processes.append(
                        {
                            "pid": int(process.pid),
                            "gpu_index": index,
                            "gpu_uuid": uuid,
                            "gpu_memory_bytes": used_memory,
                        }
                    )
                gpus.append(
                    {
                        "index": index,
                        "uuid": uuid,
                        "name": name,
                        "utilization_percent": (
                            utilization.gpu if utilization is not None else None
                        ),
                        "memory_total_bytes": (
                            memory.total if memory is not None else None
                        ),
                        "memory_used_bytes": (
                            memory.used if memory is not None else None
                        ),
                        "memory_free_bytes": (
                            memory.free if memory is not None else None
                        ),
                        "temperature_celsius": temperature,
                        "fan_percent": fan,
                        "power_watts": (
                            power_mw / 1000.0 if power_mw is not None else None
                        ),
                        "power_limit_watts": (
                            power_limit_mw / 1000.0
                            if power_limit_mw is not None
                            else None
                        ),
                        "process_count": len(gpu_processes),
                    }
                )
            except pynvml.NVMLError as exc:
                LOGGER.warning("Unable to collect GPU %s: %s", index, exc)
                continue
        self.last_error = None
        return gpus, processes
