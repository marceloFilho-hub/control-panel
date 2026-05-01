"""Gerenciador de processos — lança, monitora e mata subprocessos."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from ..config.loader import AppConfig
from ..observability.alerter import TelegramAlerter
from ..observability.logger import ExecutionLogger
from ..orchestration.state import AppState
from .python_runner import load_env_file
from .resource_monitor import get_process_metrics, get_system_metrics, kill_process_tree
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
        # ID único da execução corrente — gerado no `start()` e usado em
        # todos os alertas para correlacionar evento de Telegram, linha do
        # JSONL no Drive e log local desta run.
        self._run_id: str | None = None

    def _gerar_run_id(self) -> str:
        """Identificador único legível: <app>-<YYYYmmdd-HHMMSS>."""
        return f"{self.cfg.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    async def start(self) -> int | None:
        """Inicia o processo e retorna o PID."""
        # Gera o run_id antes de qualquer ação para que falhas mesmo no setup
        # já saiam com um identificador correlacionável.
        self._run_id = self._gerar_run_id()

        cwd = Path(self.cfg.cwd)
        if not cwd.exists():
            logger.error(f"[{self.cfg.name}] Diretório não existe: {cwd}")
            self.state.status = "failed"
            self.state.last_error = f"cwd não existe: {cwd}"
            return None

        # Base: environment atual + PYTHONUTF8 + overrides manuais
        env = {**os.environ, "PYTHONUTF8": "1"}

        # Carregar .env do projeto (se configurado)
        if self.cfg.env_file:
            try:
                loaded = load_env_file(Path(self.cfg.env_file))
                if loaded:
                    env.update(loaded)
                    logger.info(
                        f"[{self.cfg.name}] Carregadas {len(loaded)} vars de {self.cfg.env_file}"
                    )
            except Exception as e:
                logger.warning(f"[{self.cfg.name}] Falha ao carregar .env: {e}")

        # Overrides manuais do config.yaml (maior prioridade)
        env.update(self.cfg.env)

        logger.info(f"[{self.cfg.name}] Iniciando: {self.cfg.cmd} em {cwd}")

        # Abrir logger de execução (cria o arquivo por run)
        self._exec_logger = ExecutionLogger(self.cfg.name)
        self._peak_ram_mb = 0.0

        # Pre-start hooks: git_pull (best-effort) + pre_start[] (sequencial).
        # Saída unificada no .log da execução com prefixos [git]/[pre].
        hooks_ok, hook_err = await self._run_pre_start_hooks(cwd, env)
        if not hooks_ok:
            logger.error(f"[{self.cfg.name}] Hook pré-execução falhou: {hook_err}")
            self.state.status = "failed"
            self.state.last_error = f"Hook pré-execução: {hook_err}"
            self._exec_logger.close(
                status="failed",
                exit_code=-1,
                error=f"Hook pré-execução: {hook_err}",
                peak_ram_mb=0.0,
            )
            await self.alerter.alert_failure(
                self.cfg.name, -1, f"Hook pré-execução: {hook_err}", run_id=self._run_id
            )
            return None

        # Flags de criação do processo — apps GUI precisam escapar do Job
        # Object e herdar o desktop interativo do usuário. Sem isso, janelas
        # Tkinter/PyQt não aparecem (ficam no WindowStation errado).
        import subprocess as _sp
        creationflags = 0
        if self.cfg.gui:
            # CREATE_BREAKAWAY_FROM_JOB = 0x01000000
            # CREATE_NEW_PROCESS_GROUP = 0x00000200
            # DETACHED_PROCESS = 0x00000008
            creationflags = 0x01000000 | 0x00000200 | 0x00000008
            logger.info(f"[{self.cfg.name}] Modo GUI: breakaway from job + detached")

        # Usar subprocess_shell no Windows — o cmd.exe resolve PATH, aspas
        # e built-ins corretamente (cscript, powershell, etc.).
        try:
            kwargs = {
                "cwd": str(cwd),
                "env": env,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            }
            if creationflags:
                kwargs["creationflags"] = creationflags
            self._process = await asyncio.create_subprocess_shell(
                self.cfg.cmd, **kwargs
            )
        except Exception as e:
            logger.error(f"[{self.cfg.name}] Falha ao iniciar: {e}")
            self.state.status = "failed"
            self.state.last_error = str(e)
            if self._exec_logger:
                self._exec_logger.close(status="failed", exit_code=-1, error=str(e))
            await self.alerter.alert_failure(
                self.cfg.name, -1, str(e), run_id=self._run_id
            )
            return None

        self.state.pid = self._process.pid
        self.state.status = "running"
        self.state.started_at = time.time()
        self.state.last_error = ""
        self._exec_logger.record.pid = self._process.pid
        self._cleanup_done = False
        # Hooks já rodaram com sucesso antes do subprocess principal —
        # registra o resultado no record para aparecer no history.jsonl.

        # Associar ao Windows Job Object ASAP para capturar futuros filhos.
        # Exceto para apps GUI — esses precisam escapar do job pra herdar
        # o desktop interativo do usuário (senão janelas ficam invisíveis).
        if self.cfg.gui:
            logger.info(
                f"[{self.cfg.name}] GUI app — sem Job Object, cleanup por kill_tree"
            )
            self._job = None
        else:
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

    # ── Pre-start hooks (git_pull + pre_start[]) ───────────────────

    def _resolve_venv_executables(self, cwd: Path) -> tuple[str, str]:
        """Retorna (python_exe, pip_exe) preferindo o .venv do projeto.

        Procura `cwd/.venv/Scripts/python.exe` e `cwd/.venv/Scripts/pip.exe`.
        Se não existir, cai no `python`/`pip` do PATH. Pip é resolvido como
        `<python> -m pip` quando o pip.exe direto não está disponível.
        """
        venv_python = cwd / ".venv" / "Scripts" / "python.exe"
        venv_pip = cwd / ".venv" / "Scripts" / "pip.exe"

        if venv_python.exists():
            python_str = f'"{venv_python}"' if " " in str(venv_python) else str(venv_python)
            if venv_pip.exists():
                pip_str = f'"{venv_pip}"' if " " in str(venv_pip) else str(venv_pip)
            else:
                pip_str = f'{python_str} -m pip'
            return python_str, pip_str

        return "python", "python -m pip"

    async def _run_one_hook(
        self,
        cmd: str,
        cwd: Path,
        env: dict[str, str],
        prefix: bytes,
        timeout: float,
    ) -> tuple[int, str]:
        """Executa um único hook shell e devolve (returncode, last_stderr_snippet).

        Saída completa (stdout+stderr) vai pro `_exec_logger` linha a linha
        com o prefixo informado. Em caso de timeout, retorna (-1, "timeout").
        """
        if self._exec_logger is not None:
            header = f"\n[hook] $ {cmd}\n".encode("utf-8")
            self._exec_logger.write(header)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            err = f"falha ao iniciar hook: {e}"
            if self._exec_logger is not None:
                self._exec_logger.write(prefix + err.encode("utf-8") + b"\n")
            return -1, err

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            if self._exec_logger is not None:
                self._exec_logger.write(prefix + b"timeout\n")
            return -1, "timeout"

        if self._exec_logger is not None:
            for raw in (stdout_data or b"").splitlines(keepends=True):
                self._exec_logger.write(prefix + raw)
            for raw in (stderr_data or b"").splitlines(keepends=True):
                self._exec_logger.write(prefix + raw)

        rc = proc.returncode if proc.returncode is not None else -1
        last_err = ""
        if rc != 0:
            try:
                last_err = (stderr_data or b"").decode("utf-8", errors="replace")[-300:]
            except Exception:
                last_err = ""
        return rc, last_err

    async def _run_pre_start_hooks(
        self,
        cwd: Path,
        env: dict[str, str],
    ) -> tuple[bool, str]:
        """Executa git_pull (best-effort) + pre_start[] (configurável).

        Retorna (success, error_msg). `success=False` apenas quando algum
        comando do `pre_start` falhou e `pre_start_required=True`. Falha do
        `git_pull` é sempre best-effort (loga + alerta, mas retorna True).

        O timeout total (`pre_start_timeout`) cobre git_pull + todos os
        pre_start juntos. Se estourar entre comandos, aborta o restante.
        """
        cfg = self.cfg
        if not cfg.git_pull and not cfg.pre_start:
            return True, ""

        deadline = time.time() + max(1, cfg.pre_start_timeout)

        def remaining() -> float:
            return max(0.0, deadline - time.time())

        # 1) git pull (best-effort)
        if cfg.git_pull:
            if not (cwd / ".git").exists():
                logger.info(
                    f"[{cfg.name}] git_pull=true mas {cwd} não é repo git — pulando"
                )
                if self._exec_logger is not None:
                    self._exec_logger.write(
                        b"[git] cwd nao e repo git, pulando\n"
                    )
            else:
                rc, err = await self._run_one_hook(
                    "git pull --ff-only",
                    cwd,
                    env,
                    b"[git] ",
                    timeout=remaining() or 60.0,
                )
                if rc != 0:
                    msg = f"git pull falhou (rc={rc}): {err.strip()[:200]}"
                    logger.warning(f"[{cfg.name}] {msg}")
                    try:
                        await self.alerter.alert_failure(
                            cfg.name,
                            rc,
                            f"git_pull (best-effort): {msg}",
                            run_id=self._run_id,
                        )
                    except Exception:
                        pass
                else:
                    logger.info(f"[{cfg.name}] git pull OK")

        # 2) pre_start[] (sequencial, com substituição {python}/{pip})
        if cfg.pre_start:
            python_str, pip_str = self._resolve_venv_executables(cwd)
            for raw_cmd in cfg.pre_start:
                cmd = (raw_cmd or "").strip()
                if not cmd:
                    continue
                cmd = cmd.replace("{python}", python_str).replace("{pip}", pip_str)
                if remaining() <= 0:
                    msg = "timeout total dos hooks atingido"
                    logger.error(f"[{cfg.name}] {msg}")
                    if cfg.pre_start_required:
                        return False, msg
                    return True, ""

                rc, err = await self._run_one_hook(
                    cmd,
                    cwd,
                    env,
                    b"[pre] ",
                    timeout=remaining(),
                )
                if rc != 0:
                    msg = f"comando falhou (rc={rc}): {cmd} | {err.strip()[:200]}"
                    if cfg.pre_start_required:
                        logger.error(f"[{cfg.name}] {msg}")
                        return False, msg
                    logger.warning(f"[{cfg.name}] (não-bloqueante) {msg}")

        return True, ""

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
                    await self.alerter.alert_timeout(
                        self.cfg.name, timeout, run_id=self._run_id
                    )
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
                        self.cfg.name, metrics.ram_mb, max_ram, run_id=self._run_id
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
                    from ..observability.logger import read_log_content
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
                self.cfg.name, exit_code, err_snippet, run_id=self._run_id
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
