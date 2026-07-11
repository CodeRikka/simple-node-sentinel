from __future__ import annotations

import logging
import os
from typing import Any

import psutil

LOGGER = logging.getLogger(__name__)

VIRTUAL_FILESYSTEMS = {
    "proc",
    "sysfs",
    "tmpfs",
    "devtmpfs",
    "devpts",
    "cgroup",
    "cgroup2",
    "overlay",
    "squashfs",
    "securityfs",
    "debugfs",
    "tracefs",
    "pstore",
    "autofs",
    "mqueue",
    "hugetlbfs",
    "fusectl",
    "configfs",
}


def collect_cpu_temperature() -> dict[str, Any]:
    try:
        groups = psutil.sensors_temperatures(fahrenheit=False) or {}
    except (AttributeError, OSError) as exc:
        LOGGER.debug("CPU temperatures are unavailable: %s", exc)
        groups = {}
    sensors: list[dict[str, Any]] = []
    for source, entries in groups.items():
        for entry in entries:
            if entry.current is None:
                continue
            sensors.append(
                {
                    "source": source,
                    "label": entry.label or source,
                    "current_celsius": entry.current,
                    "high_celsius": entry.high,
                    "critical_celsius": entry.critical,
                }
            )
    return {
        "available": bool(sensors),
        "max_celsius": (
            max(sensor["current_celsius"] for sensor in sensors)
            if sensors
            else None
        ),
        "sensors": sensors,
    }


def collect_system_summary() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    return {
        "cpu": {
            "usage_percent": psutil.cpu_percent(interval=None),
            "per_cpu_percent": psutil.cpu_percent(interval=None, percpu=True),
            "logical_count": psutil.cpu_count(logical=True),
            "load_average": {"1m": load[0], "5m": load[1], "15m": load[2]},
            "boot_time": psutil.boot_time(),
        },
        "cpu_temperature": collect_cpu_temperature(),
        "memory": {
            "total_bytes": memory.total,
            "used_bytes": memory.used,
            "available_bytes": memory.available,
            "usage_percent": memory.percent,
        },
        "swap": {
            "total_bytes": swap.total,
            "used_bytes": swap.used,
            "usage_percent": swap.percent,
        },
    }


def collect_disks() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    seen_mounts: set[str] = set()
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError as exc:
        LOGGER.warning("Unable to enumerate disks: %s", exc)
        return []
    for partition in partitions:
        if (
            partition.fstype.lower() in VIRTUAL_FILESYSTEMS
            or partition.mountpoint in seen_mounts
        ):
            continue
        seen_mounts.add(partition.mountpoint)
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (OSError, PermissionError) as exc:
            LOGGER.warning(
                "Unable to read mount %s: %s", partition.mountpoint, exc
            )
            continue
        disks.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "available_bytes": usage.free,
                "usage_percent": usage.percent,
            }
        )
    return sorted(disks, key=lambda item: item["mountpoint"])
