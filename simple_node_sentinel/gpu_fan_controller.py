from __future__ import annotations

import ctypes
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)
NVML_SUCCESS = 0
UUID_BUFFER_SIZE = 96


class FanControlError(RuntimeError):
    pass


class GpuFanController:
    def __init__(self) -> None:
        self.library: Any | None = None
        self.initialized = False
        self.last_error: str | None = None

    def initialize(self) -> None:
        try:
            library = ctypes.CDLL("libnvidia-ml.so.1")
            self._configure_signatures(library)
            self._check(library.nvmlInit_v2(), "initialize NVML", library)
            self.library = library
            self.initialized = True
            self.last_error = None
        except (OSError, AttributeError, FanControlError) as exc:
            self.last_error = str(exc)
            LOGGER.warning("GPU fan control is unavailable: %s", exc)

    def close(self) -> None:
        if self.initialized and self.library is not None:
            result = self.library.nvmlShutdown()
            if result != NVML_SUCCESS:
                LOGGER.warning("Unable to shut down fan-control NVML: %s", self._error(result))
        self.initialized = False
        self.library = None

    @staticmethod
    def _configure_signatures(library: Any) -> None:
        device = ctypes.c_void_p
        library.nvmlInit_v2.restype = ctypes.c_int
        library.nvmlShutdown.restype = ctypes.c_int
        library.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        library.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        library.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(device),
        ]
        library.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        library.nvmlDeviceGetUUID.argtypes = [
            device,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        library.nvmlDeviceGetUUID.restype = ctypes.c_int
        library.nvmlDeviceGetNumFans.argtypes = [
            device,
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.nvmlDeviceGetNumFans.restype = ctypes.c_int
        library.nvmlDeviceSetFanSpeed_v2.argtypes = [
            device,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        library.nvmlDeviceSetFanSpeed_v2.restype = ctypes.c_int
        library.nvmlDeviceSetDefaultFanSpeed_v2.argtypes = [
            device,
            ctypes.c_uint,
        ]
        library.nvmlDeviceSetDefaultFanSpeed_v2.restype = ctypes.c_int
        library.nvmlErrorString.argtypes = [ctypes.c_int]
        library.nvmlErrorString.restype = ctypes.c_char_p

    def _error(self, result: int) -> str:
        if self.library is None:
            return f"NVML error {result}"
        value = self.library.nvmlErrorString(result)
        if not value:
            return f"NVML error {result}"
        return value.decode("utf-8", errors="replace")

    def _check(self, result: int, operation: str, library: Any | None = None) -> None:
        if result == NVML_SUCCESS:
            return
        if library is not None and self.library is None:
            value = library.nvmlErrorString(result)
            detail = (
                value.decode("utf-8", errors="replace")
                if value
                else f"NVML error {result}"
            )
        else:
            detail = self._error(result)
        raise FanControlError(f"Unable to {operation}: {detail}")

    def _require_library(self) -> Any:
        if not self.initialized or self.library is None:
            raise FanControlError(self.last_error or "GPU fan control is not initialized")
        return self.library

    def _handle_by_uuid(self, gpu_uuid: str) -> ctypes.c_void_p:
        library = self._require_library()
        count = ctypes.c_uint()
        self._check(library.nvmlDeviceGetCount_v2(ctypes.byref(count)), "list GPUs")
        for index in range(count.value):
            handle = ctypes.c_void_p()
            self._check(
                library.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)),
                f"open GPU {index}",
            )
            buffer = ctypes.create_string_buffer(UUID_BUFFER_SIZE)
            self._check(
                library.nvmlDeviceGetUUID(handle, buffer, UUID_BUFFER_SIZE),
                f"read UUID for GPU {index}",
            )
            if buffer.value.decode("utf-8", errors="replace") == gpu_uuid:
                return handle
        raise FanControlError(f"GPU {gpu_uuid} is no longer available")

    def _fan_count(self, handle: ctypes.c_void_p) -> int:
        library = self._require_library()
        count = ctypes.c_uint()
        self._check(library.nvmlDeviceGetNumFans(handle, ctypes.byref(count)), "list fans")
        if count.value == 0:
            raise FanControlError("GPU reports no controllable fans")
        return count.value

    def supports(self, gpu_uuid: str) -> tuple[bool, str | None]:
        try:
            self._fan_count(self._handle_by_uuid(gpu_uuid))
            return True, None
        except FanControlError as exc:
            return False, str(exc)

    def set_manual(self, gpu_uuid: str, percent: int) -> None:
        library = self._require_library()
        handle = self._handle_by_uuid(gpu_uuid)
        fan_count = self._fan_count(handle)
        try:
            for fan_index in range(fan_count):
                self._check(
                    library.nvmlDeviceSetFanSpeed_v2(handle, fan_index, percent),
                    f"set fan {fan_index} to {percent}%",
                )
        except FanControlError:
            for fan_index in range(fan_count):
                library.nvmlDeviceSetDefaultFanSpeed_v2(handle, fan_index)
            raise

    def set_auto(self, gpu_uuid: str) -> None:
        library = self._require_library()
        handle = self._handle_by_uuid(gpu_uuid)
        fan_count = self._fan_count(handle)
        for fan_index in range(fan_count):
            self._check(
                library.nvmlDeviceSetDefaultFanSpeed_v2(handle, fan_index),
                f"restore automatic control for fan {fan_index}",
            )
