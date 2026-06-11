"""Host system statistics for the dashboard."""

from __future__ import annotations

import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class SystemStats:
    cpu_percent: float
    load_avg: tuple[float, float, float]
    memory_total: int
    memory_used: int
    memory_percent: float
    disk_total: int
    disk_used: int
    disk_percent: float
    uptime_seconds: int


def collect() -> SystemStats:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return SystemStats(
        cpu_percent=psutil.cpu_percent(interval=None),
        load_avg=psutil.getloadavg(),
        memory_total=mem.total,
        memory_used=mem.used,
        memory_percent=mem.percent,
        disk_total=disk.total,
        disk_used=disk.used,
        disk_percent=disk.percent,
        uptime_seconds=max(0, int(time.time() - psutil.boot_time())),
    )
