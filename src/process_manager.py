"""Gerenciador de processos — lança, monitora e mata subprocessos."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from loguru import logger

from .alerter import TelegramAlerter
from .config_loader import AppConfig
from .execution_logger import ExecutionLogger
from .resource_monitor import get_process_metrics, get_system_metrics, kill_process_tree
from .state import AppState
from .windows_job import JobObject


class ProcessManager:
    """Gerencia o ciclo de vida de um subprocess de app."""

    def __init__(self, app_cfg: AppConfig, app_state: AppState, alerter: TelegramAlerter):
        self.cfg = app_cfg
        self.state = app_state
        self.alerter = alerter
        self._process: asyncio.subprocess.Process | None = None
        self._exec_logger: ExecutionLogger | None = None
        self._peak_ram_mb: float = 0.0
        self._job: JobObject | None = None
        self._cleanup_done: bool = False

    async def start(self) -> int | None:
        """Inicia o processo e retorna o PID."""
        cwd = Path(self.cfg.cwd)
        if not cwd.exists():
            logger.error(f"[{self.cfg.name}] Diretório não existe: {cwd}")
            self.state.status = "failed"
            self.state.last_error = f"cwd não existe: {cwd}"
            return None

        env = {**os.environ, "PYTHONUTF8": "1", **self.cfg.env}

        logger.info(f"[{self.cfg.name}] Iniciando: {self.cfg.cmd} em {cwd}")

        # Abrir logger de execução (cria o arquivo por run)
        self._exec_logger = ExecutionLogger(self.cfg.name)
        self._peak_ram_mb = 0.0

        # Usar subprocess_shell no Windows — o cmd.exe resolve PATH, aspas
        # e built-ins corretamente (cscript, powershell, etc.).
        # create_subprocess_exec dá [WinError 5] Access Denied pra cmds
        # com aspas literais ou sem caminho absoluto.
        try:
            self._process = await asyncio.create_subprocess_shell(
                self.cfg.cmd,
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
        self._cleanup_done = False

        # Associar ao Windows Job Object ASAP para capturar futuros filhos.
        # Isso garante que netos/bisnetos spawnados pelo processo também
        # morrerão quando o job for fechado (cleanup garantido).
        self._job = JobObject(name=self.cfg.name)
        if self._job.available:
            if self._job.assign(self._process.pid):
                logger.debug(f"[{self.cfg.name}] PID {self._process.pid} associado ao Job Object")
            else:
                logger.warning(f"[{self.cfg.name}] Falha ao associar ao Job Object — kill_tree será usado como fallback")

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

    def _finalize(self) -> None:
        """Garante que TODA a árvore do processo morreu e libera a memória.

        Chamado em todos os paths de término (sucesso, falha, timeout, ram
        excedida, kill manual). Idempotente — chamar duas vezes é seguro.

        Sequência:
          1) Terminar Job Object (mata árvore atômica no Windows)
          2) Failsafe via psutil.kill_process_tree caso o job tenha falhado
          3) gc.collect() para liberar memória do próprio orchestrator
          4) Log do resultado
        """
        if self._cleanup_done:
            return
        self._cleanup_done = True

        ram_before = get_system_metrics()["available_ram_mb"]
        killed_count = 0

        # 1) Job Object — mata árvore inteira atomicamente
        if self._job and self._job.available:
            killed_count = self._job.terminate()
            self._job.close()
            self._job = None

        # 2) Failsafe — kill_tree via psutil caso sobre alguém
        if self.state.pid:
            metrics = get_process_metrics(self.state.pid)
            if metrics.alive:
                logger.warning(
                    f"[{self.cfg.name}] PID {self.state.pid} ainda vivo após job — kill_tree"
                )
                kill_process_tree(self.state.pid)
                killed_count += 1

        # 3) Forçar GC do orchestrator (libera memória Python residual)
        import gc
        gc.collect()

        # 4) Log
        ram_after = get_system_metrics()["available_ram_mb"]
        freed = ram_after - ram_before
        if killed_count > 0 or freed > 10:
            logger.info(
                f"[{self.cfg.name}] Cleanup: {killed_count} proc(s) extintos, "
                f"~{freed:.0f} MB liberados (RAM livre: {ram_after / 1024:.2f} GB)"
            )

    def _measure_tree_ram(self) -> float:
        """Soma a RAM de TODOS os processos do Job Object (árvore inteira)."""
        if not self._job or not self._job.available:
            return 0.0
        import psutil
        total = 0.0
        for pid in self._job._assigned_pids:
            try:
                p = psutil.Process(pid)
                if p.is_running():
                    total += p.memory_info().rss / (1024 * 1024)
                for c in p.children(recursive=True):
                    try:
                        if c.is_running():
                            total += c.memory_info().rss / (1024 * 1024)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    async def _wait_for_descendants(
        self, start_time: float, timeout: int, max_ram: int, original_pid: int
    ) -> None:
        """Aguarda descendentes no Job Object terminarem.

        Suporta padrão 'launcher' — VBS/BAT que lança um processo filho
        (ex: pythonw.exe) e termina logo após. Sem isso, o Job Object
        mataria o filho junto com o launcher.

        Sai quando:
          - Todos os processos do job terminaram, OU
          - Estado foi marcado como disabled (usuário clicou em stop), OU
          - Timeout total excedido, OU
          - RAM da árvore excedida
        """
        if not self._job or not self._job.available:
            return

        alive = self._job.count_alive()
        if alive <= 0:
            return  # nada a esperar

        logger.info(
            f"[{self.cfg.name}] Processo principal (PID {original_pid}) terminou, "
            f"mas {alive} descendente(s) ainda rodando — aguardando finalizarem..."
        )
        # Restaura status "running" (os descendentes ainda estão ativos)
        self.state.status = "running"

        while self._job and self._job.available:
            alive = self._job.count_alive()
            if alive <= 0:
                break

            if not self.state.enabled:
                logger.info(f"[{self.cfg.name}] Parada solicitada — encerrando descendentes")
                break

            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning(
                    f"[{self.cfg.name}] Timeout total ({timeout}s) — matando descendentes"
                )
                self.state.status = "timeout"
                self.state.last_error = f"Timeout total após {elapsed:.0f}s"
                break

            # Monitorar RAM da árvore toda
            tree_ram = self._measure_tree_ram()
            self.state.ram_mb = tree_ram
            if tree_ram > self._peak_ram_mb:
                self._peak_ram_mb = tree_ram
            if tree_ram > max_ram:
                logger.warning(
                    f"[{self.cfg.name}] RAM da árvore excedida: "
                    f"{tree_ram:.0f}MB > {max_ram}MB — matando"
                )
                self.state.status = "failed"
                self.state.last_error = f"RAM excedida (árvore): {tree_ram:.0f}MB"
                break

            await asyncio.sleep(3)

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
                    self.state.status = "timeout"
                    self.state.last_error = f"Timeout após {elapsed:.0f}s"
                    if self._exec_logger:
                        self._exec_logger.close(
                            status="timeout",
                            exit_code=-1,
                            error=f"Timeout após {elapsed:.0f}s",
                            peak_ram_mb=self._peak_ram_mb,
                        )
                    self._finalize()
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
                    self.state.status = "failed"
                    self.state.last_error = f"RAM excedida: {metrics.ram_mb:.0f}MB"
                    if self._exec_logger:
                        self._exec_logger.close(
                            status="failed",
                            exit_code=-1,
                            error=f"RAM excedida: {metrics.ram_mb:.0f}MB",
                            peak_ram_mb=self._peak_ram_mb,
                        )
                    self._finalize()
                    await self.alerter.alert_ram(
                        self.cfg.name, metrics.ram_mb, max_ram
                    )
                    return -1

        except Exception as e:
            logger.error(f"[{self.cfg.name}] Erro no monitoramento: {e}")

        # ── Fase 2: Processo principal terminou, mas pode ser um LAUNCHER
        # (ex: VBS que lança pythonw.exe e sai). Aguardar os descendentes
        # no Job Object até todos terminarem, respeitando timeout/RAM/stop.
        await self._wait_for_descendants(start_time, timeout, max_ram, pid)

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

        # SEMPRE finalizar — mesmo em sucesso o processo pode ter deixado
        # descendentes órfãos (browsers Selenium, subprocess detached, etc.)
        self._finalize()

        return exit_code

    def is_alive(self) -> bool:
        """Verifica se o processo está vivo."""
        if self._process is None or self.state.pid is None:
            return False
        metrics = get_process_metrics(self.state.pid)
        return metrics.alive

    def kill(self) -> None:
        """Mata o processo e TODA a árvore de descendentes via Job Object."""
        if self._exec_logger:
            self._exec_logger.close(
                status="killed",
                exit_code=None,
                error="Encerrado manualmente",
                peak_ram_mb=self._peak_ram_mb,
            )
            self._exec_logger = None
        # _finalize() lida com Job Object + failsafe psutil + gc
        self._finalize()
        self.state.status = "off"
        self.state.pid = None
        self.state.ram_mb = 0
        self.state.cpu_pct = 0
