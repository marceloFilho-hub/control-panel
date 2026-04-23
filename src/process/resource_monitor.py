"""Monitor de recursos por processo via psutil."""

from __future__ import annotations

from dataclasses import dataclass

import psutil
from loguru import logger


@dataclass
class ProcessMetrics:
    pid: int
    alive: bool
    ram_mb: float
    cpu_pct: float


def get_process_metrics(pid: int) -> ProcessMetrics:
    """Retorna métricas de RAM e CPU de um processo."""
    try:
        proc = psutil.Process(pid)
        mem = proc.memory_info()
        cpu = proc.cpu_percent(interval=0)
        # Soma filhos (subprocessos do app)
        ram_total = mem.rss
        for child in proc.children(recursive=True):
            try:
                ram_total += child.memory_info().rss
                cpu += child.cpu_percent(interval=0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return ProcessMetrics(
            pid=pid,
            alive=True,
            ram_mb=ram_total / (1024 * 1024),
            cpu_pct=cpu,
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ProcessMetrics(pid=pid, alive=False, ram_mb=0, cpu_pct=0)


def kill_process_tree(pid: int) -> bool:
    """Mata um processo e todos os seus filhos."""
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        proc.kill()
        logger.info(f"Processo {pid} e {len(children)} filhos encerrados")
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.warning(f"Não foi possível matar PID {pid}: {e}")
        return False


def get_system_metrics() -> dict:
    """Retorna métricas globais do sistema."""
    vm = psutil.virtual_memory()
    return {
        "total_ram_mb": vm.total / (1024 * 1024),
        "used_ram_mb": vm.used / (1024 * 1024),
        "available_ram_mb": vm.available / (1024 * 1024),
        "cpu_pct": psutil.cpu_percent(interval=0),
        "cpu_count": psutil.cpu_count(),
    }
