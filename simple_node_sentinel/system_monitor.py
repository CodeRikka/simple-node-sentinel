from __future__ import annotations

import logging
import os
from pathlib import Path
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


def _read_sysfs_value(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _physical_block_names(device: str) -> list[str]:
    try:
        block_name = Path(device).resolve().name
    except OSError:
        block_name = Path(device).name
    block_path = Path("/sys/class/block") / block_name
    if not block_path.exists():
        return []

    try:
        resolved = block_path.resolve()
    except OSError:
        resolved = block_path
    if (block_path / "partition").exists():
        block_name = resolved.parent.name
        block_path = Path("/sys/class/block") / block_name

    try:
        slaves = sorted((block_path / "slaves").iterdir())
    except OSError:
        slaves = []
    if not slaves:
        return [block_name]

    names: set[str] = set()
    for slave in slaves:
        names.update(_physical_block_names(f"/dev/{slave.name}"))
    return sorted(names) or [block_name]


def physical_disk_info(device: str) -> list[dict[str, Any]]:
    disks = []
    for name in _physical_block_names(device):
        path = Path("/sys/class/block") / name
        sectors = _read_sysfs_value(path / "size")
        rotational = _read_sysfs_value(path / "queue" / "rotational")
        disks.append(
            {
                "name": name,
                "device": f"/dev/{name}",
                "model": _read_sysfs_value(path / "device" / "model"),
                "size_bytes": int(sectors) * 512 if sectors and sectors.isdigit() else None,
                "rotational": (
                    rotational == "1" if rotational in {"0", "1"} else None
                ),
            }
        )
    return disks


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
                "physical_disks": physical_disk_info(partition.device),
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "available_bytes": usage.free,
                "usage_percent": usage.percent,
            }
        )
    return sorted(disks, key=lambda item: item["mountpoint"])
