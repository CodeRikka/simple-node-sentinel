from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

LOGGER = logging.getLogger(__name__)

SENSITIVE_OPTIONS = {
    "--password",
    "--passwd",
    "--token",
    "--api-key",
    "--apikey",
    "--secret",
    "--access-key",
    "--wandb-api-key",
    "--hf-token",
}


@lru_cache(maxsize=1)
def login_uid_min() -> int:
    try:
        lines = Path("/etc/login.defs").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return 1000
    for line in lines:
        values = line.split("#", 1)[0].split()
        if len(values) == 2 and values[0] == "UID_MIN":
            try:
                return int(values[1])
            except ValueError:
                break
    return 1000


def is_primary_user(username: str, uid: int | None) -> bool:
    if username == "root" or uid == 0:
        return True
    return uid is not None and login_uid_min() <= uid < 65534


def _new_user_summary(username: str, uid: int | None) -> dict[str, Any]:
    return {
        "username": username,
        "uid": uid,
        "is_primary": is_primary_user(username, uid),
        "process_count": 0,
        "cpu_percent": 0.0,
        "memory_rss_bytes": 0,
        "gpu_process_count": 0,
        "gpu_memory_bytes": 0,
    }


def sanitize_command(args: Iterable[str]) -> str:
    values = list(args)
    sanitized: list[str] = []
    hide_next = False
    for value in values:
        if hide_next:
            sanitized.append("********")
            hide_next = False
            continue
        option, separator, _secret = value.partition("=")
        if option.lower() in SENSITIVE_OPTIONS:
            if separator:
                sanitized.append(f"{option}=********")
            else:
                sanitized.append(value)
                hide_next = True
        else:
            sanitized.append(value)
    return " ".join(sanitized)


class ProcessMonitor:
    def __init__(self) -> None:
        self._process_cache: dict[tuple[int, float], psutil.Process] = {}
        self._cpu_samples: dict[tuple[int, float], tuple[int, float]] = {}
        self._cycle = 0

    def _get_process(
        self, pid: int
    ) -> tuple[psutil.Process, float, tuple[int, float]]:
        process = psutil.Process(pid)
        created = process.create_time()
        key = (pid, created)
        cached = self._process_cache.get(key)
        if cached is None:
            self._process_cache[key] = process
            cached = process
        return cached, created, key

    def _cpu_percent(
        self, process: psutil.Process, key: tuple[int, float]
    ) -> float:
        sample = self._cpu_samples.get(key)
        if sample is not None and sample[0] == self._cycle:
            return sample[1]
        value = process.cpu_percent(interval=None)
        self._cpu_samples[key] = (self._cycle, value)
        return value

    def inspect_gpu_process(
        self,
        gpu_process: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any] | None:
        pid = int(gpu_process["pid"])
        try:
            process, created, key = self._get_process(pid)
            with process.oneshot():
                username = process.username()
                uid = process.uids().real
                command_parts = process.cmdline()
                name = process.name()
                rss = process.memory_info().rss
                cpu_percent = self._cpu_percent(process, key)
            current_time = now if now is not None else time.time()
            return {
                "pid": pid,
                "username": username,
                "uid": uid,
                "command": sanitize_command(command_parts),
                "executable": name,
                "started_at": created,
                "runtime_seconds": max(0.0, current_time - created),
                "cpu_percent": cpu_percent,
                "memory_rss_bytes": rss,
                "gpu_index": gpu_process["gpu_index"],
                "gpu_uuid": gpu_process["gpu_uuid"],
                "gpu_memory_bytes": gpu_process.get("gpu_memory_bytes"),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
            return None
        except (OSError, ValueError) as exc:
            LOGGER.debug("Unable to read GPU process PID %s: %s", pid, exc)
            return None

    def inspect_gpu_processes(
        self, gpu_processes: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._cycle += 1
        now = time.time()
        results = []
        for gpu_process in gpu_processes:
            detail = self.inspect_gpu_process(gpu_process, now)
            if detail is not None:
                results.append(detail)
        return results

    def user_summary(
        self, gpu_processes: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        active_keys: set[tuple[int, float]] = set()
        for process in psutil.process_iter(["pid", "username", "uids", "memory_info"]):
            try:
                created = process.create_time()
                key = (process.pid, created)
                active_keys.add(key)
                cached = self._process_cache.setdefault(key, process)
                username = process.info.get("username") or cached.username()
                uids = process.info.get("uids")
                uid = uids.real if uids is not None else cached.uids().real
                memory = process.info.get("memory_info")
                rss = memory.rss if memory is not None else cached.memory_info().rss
                cpu = self._cpu_percent(cached, key)
                row = summaries.setdefault(
                    username,
                    _new_user_summary(username, uid),
                )
                row["process_count"] += 1
                row["cpu_percent"] += cpu
                row["memory_rss_bytes"] += rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
                continue
            except (OSError, ValueError) as exc:
                LOGGER.debug("Unable to aggregate PID %s: %s", process.pid, exc)

        seen_gpu_pids: dict[str, set[int]] = {}
        for process in gpu_processes:
            username = process.get("username")
            if not username:
                continue
            uid = process.get("uid")
            row = summaries.setdefault(
                username,
                _new_user_summary(username, uid),
            )
            user_pids = seen_gpu_pids.setdefault(username, set())
            if process["pid"] not in user_pids:
                user_pids.add(process["pid"])
                row["gpu_process_count"] += 1
            row["gpu_memory_bytes"] += process.get("gpu_memory_bytes") or 0

        self._process_cache = {
            key: process
            for key, process in self._process_cache.items()
            if key in active_keys
        }
        self._cpu_samples = {
            key: sample
            for key, sample in self._cpu_samples.items()
            if key in active_keys
        }
        return sorted(summaries.values(), key=lambda item: item["username"])
