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
from .execution_logger import ExecutionLogger
from .resource_monitor import get_process_metrics, kill_process_tree
from .state import AppState


class ProcessManager:
    """Gerencia o ciclo de vida de um subprocess de app."""

    def __init__(self, app_cfg: AppConfig, app_state: AppState, alerter: TelegramAlerter):
        self.cfg = app_cfg
        self.state = app_state
        self.alerter = alerter
        self._process: asyncio.subprocess.Process | None = None
        self._exec_logger: ExecutionLogger | None = None
        self._peak_ram_mb: float = 0.0

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

        # Abrir logger de execução (cria o arquivo por run)
        self._exec_logger = ExecutionLogger(self.cfg.name)
        self._peak_ram_mb = 0.0

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
            if self._exec_logger:
                self._exec_logger.close(status="failed", exit_code=-1, error=str(e))
            await self.alerter.alert_failure(self.cfg.name, -1, str(e))
            return None

        self.state.pid = self._process.pid
        self.state.status = "running"
        self.state.started_at = time.time()
        self.state.last_error = ""
        self._exec_logger.record.pid = self._process.pid

        # Stream stdout/stderr para o arquivo de log em tempo real
        asyncio.create_task(self._stream_output(self._process.stdout, b"[out] "))
        asyncio.create_task(self._stream_output(self._process.stderr, b"[err] "))

        logger.info(f"[{self.cfg.name}] PID {self._process.pid} iniciado")
        return self._process.pid

    async def _stream_output(self, stream: asyncio.StreamReader | None, prefix: bytes) -> None:
        """Lê a stream do subprocesso linha a linha e grava no log."""
        if stream is None or self._exec_logger is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                self._exec_logger.write(prefix + line)
        except Exception:
            pass

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
                    if self._exec_logger:
                        self._exec_logger.close(
                            status="timeout",
                            exit_code=-1,
                            error=f"Timeout após {elapsed:.0f}s",
                            peak_ram_mb=self._peak_ram_mb,
                        )
                    await self.alerter.alert_timeout(self.cfg.name, timeout)
                    return -1

                # Check RAM
                metrics = get_process_metrics(pid)
                if not metrics.alive:
                    break

                self.state.ram_mb = metrics.ram_mb
                self.state.cpu_pct = metrics.cpu_pct
                if metrics.ram_mb > self._peak_ram_mb:
                    self._peak_ram_mb = metrics.ram_mb

                if metrics.ram_mb > max_ram:
                    logger.warning(
                        f"[{self.cfg.name}] RAM excedida: {metrics.ram_mb:.0f}MB > {max_ram}MB"
                    )
                    kill_process_tree(pid)
                    self.state.status = "failed"
                    self.state.last_error = f"RAM excedida: {metrics.ram_mb:.0f}MB"
                    if self._exec_logger:
                        self._exec_logger.close(
                            status="failed",
                            exit_code=-1,
                            error=f"RAM excedida: {metrics.ram_mb:.0f}MB",
                            peak_ram_mb=self._peak_ram_mb,
                        )
                    await self.alerter.alert_ram(
                        self.cfg.name, metrics.ram_mb, max_ram
                    )
                    return -1

        except Exception as e:
            logger.error(f"[{self.cfg.name}] Erro no monitoramento: {e}")

        # Coletar resultado
        exit_code = self._process.returncode or 0
        duration = time.time() - start_time

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
            if self._exec_logger:
                self._exec_logger.close(
                    status="done", exit_code=0, peak_ram_mb=self._peak_ram_mb
                )
        else:
            self.state.status = "failed"
            self.state.fail_count += 1
            # Ler últimas linhas do log pra mensagem de erro
            err_snippet = ""
            if self._exec_logger:
                try:
                    from .execution_logger import read_log_content
                    content = read_log_content(self._exec_logger.record.log_file, tail_kb=4)
                    err_lines = [l for l in content.splitlines() if l.startswith("[err]")]
                    err_snippet = "\n".join(err_lines[-20:])[:500]
                except Exception:
                    pass
            self.state.last_error = err_snippet or f"Exit code {exit_code}"
            logger.error(
                f"[{self.cfg.name}] Falhou em {duration:.1f}s (exit {exit_code})"
            )
            if self._exec_logger:
                self._exec_logger.close(
                    status="failed",
                    exit_code=exit_code,
                    error=err_snippet,
                    peak_ram_mb=self._peak_ram_mb,
                )
            await self.alerter.alert_failure(
                self.cfg.name, exit_code, err_snippet
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
        if self._exec_logger:
            self._exec_logger.close(
                status="killed",
                exit_code=None,
                error="Encerrado manualmente",
                peak_ram_mb=self._peak_ram_mb,
            )
            self._exec_logger = None
