"""Gerenciador de processos — lança, monitora e mata subprocessos."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from pathlib import Path

from loguru import logger

from .alerter import TelegramAlerter
from .config_loader import AppConfig
from .resource_monitor import get_process_metrics, kill_process_tree
from .state import AppState


class ProcessManager:
    """Gerencia o ciclo de vida de um subprocess de app."""

    def __init__(self, app_cfg: AppConfig, app_state: AppState, alerter: TelegramAlerter):
        self.cfg = app_cfg
        self.state = app_state
        self.alerter = alerter
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> int | None:
        """Inicia o processo e retorna o PID."""
        cwd = Path(self.cfg.cwd)
        if not cwd.exists():
            logger.error(f"[{self.cfg.name}] Diretório não existe: {cwd}")
            self.state.status = "failed"
            self.state.last_error = f"cwd não existe: {cwd}"
            return None

        parts = shlex.split(self.cfg.cmd, posix=False)
        cmd_path = cwd / parts[0]

        # Resolve .venv paths no Windows
        if not cmd_path.exists() and "/" in parts[0]:
            # Tenta path absoluto como está
            cmd_path = Path(parts[0])

        env = {**os.environ, "PYTHONUTF8": "1", **self.cfg.env}

        logger.info(f"[{self.cfg.name}] Iniciando: {self.cfg.cmd} em {cwd}")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *parts,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error(f"[{self.cfg.name}] Falha ao iniciar: {e}")
            self.state.status = "failed"
            self.state.last_error = str(e)
            await self.alerter.alert_failure(self.cfg.name, -1, str(e))
            return None

        self.state.pid = self._process.pid
        self.state.status = "running"
        self.state.started_at = time.time()
        self.state.last_error = ""
        logger.info(f"[{self.cfg.name}] PID {self._process.pid} iniciado")
        return self._process.pid

    async def wait_with_monitoring(self) -> int:
        """Aguarda o processo terminar, monitorando RAM e timeout."""
        if self._process is None:
            return -1

        pid = self._process.pid
        start_time = time.time()
        timeout = self.cfg.timeout
        max_ram = self.cfg.max_ram_mb

        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._process.wait(), timeout=5.0
                    )
                    # Processo terminou
                    break
                except asyncio.TimeoutError:
                    pass  # Ainda rodando — checar métricas

                elapsed = time.time() - start_time

                # Check timeout
                if elapsed > timeout:
                    logger.warning(
                        f"[{self.cfg.name}] TIMEOUT após {elapsed:.0f}s (limite: {timeout}s)"
                    )
                    kill_process_tree(pid)
                    self.state.status = "timeout"
                    self.state.last_error = f"Timeout após {elapsed:.0f}s"
                    await self.alerter.alert_timeout(self.cfg.name, timeout)
                    return -1

                # Check RAM
                metrics = get_process_metrics(pid)
                if not metrics.alive:
                    break

                self.state.ram_mb = metrics.ram_mb
                self.state.cpu_pct = metrics.cpu_pct

                if metrics.ram_mb > max_ram:
                    logger.warning(
                        f"[{self.cfg.name}] RAM excedida: {metrics.ram_mb:.0f}MB > {max_ram}MB"
                    )
                    kill_process_tree(pid)
                    self.state.status = "failed"
                    self.state.last_error = f"RAM excedida: {metrics.ram_mb:.0f}MB"
                    await self.alerter.alert_ram(
                        self.cfg.name, metrics.ram_mb, max_ram
                    )
                    return -1

        except Exception as e:
            logger.error(f"[{self.cfg.name}] Erro no monitoramento: {e}")

        # Coletar resultado
        exit_code = self._process.returncode or 0
        duration = time.time() - start_time

        # Capturar stderr para diagnóstico
        stderr_output = ""
        if self._process.stderr:
            try:
                stderr_bytes = await asyncio.wait_for(
                    self._process.stderr.read(), timeout=5.0
                )
                stderr_output = stderr_bytes.decode("utf-8", errors="replace")[-2000:]
            except Exception:
                pass

        self.state.finished_at = time.time()
        self.state.last_duration_s = duration
        self.state.last_exit_code = exit_code
        self.state.ram_mb = 0
        self.state.cpu_pct = 0
        self.state.pid = None
        self.state.run_count += 1

        if exit_code == 0:
            self.state.status = "done"
            logger.info(
                f"[{self.cfg.name}] Concluído em {duration:.1f}s (exit 0)"
            )
        else:
            self.state.status = "failed"
            self.state.fail_count += 1
            self.state.last_error = stderr_output[:500] or f"Exit code {exit_code}"
            logger.error(
                f"[{self.cfg.name}] Falhou em {duration:.1f}s (exit {exit_code})"
            )
            await self.alerter.alert_failure(
                self.cfg.name, exit_code, stderr_output[:500]
            )

        return exit_code

    def is_alive(self) -> bool:
        """Verifica se o processo está vivo."""
        if self._process is None or self.state.pid is None:
            return False
        metrics = get_process_metrics(self.state.pid)
        return metrics.alive

    def kill(self) -> None:
        """Mata o processo."""
        if self.state.pid:
            kill_process_tree(self.state.pid)
            self.state.status = "off"
            self.state.pid = None
            self.state.ram_mb = 0
            self.state.cpu_pct = 0
