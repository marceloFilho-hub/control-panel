"""Orquestrador central — scheduler + semáforos + ciclo de vida.

Modo de operação: inicia IDLE. Apps só rodam quando ativados pela UI
ou quando marcados com auto_start: true no config.yaml.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .alerter import TelegramAlerter
from .config_loader import AppConfig, ControlPlaneConfig
from .process_manager import ProcessManager
from .resource_monitor import get_system_metrics
from .state import (
    AppState,
    Command,
    ControlPlaneState,
    read_commands,
    save_state,
)


class Orchestrator:
    def __init__(self, config: ControlPlaneConfig):
        self.config = config
        self.alerter = TelegramAlerter(
            config.alerts.telegram_bot_token,
            config.alerts.telegram_chat_id,
        )
        self.scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
        self.heavy_sem = asyncio.Semaphore(config.heavy_slots)
        self.light_sem = asyncio.Semaphore(config.light_slots)
        self.state = ControlPlaneState(
            started_at=time.time(),
            heavy_slots_max=config.heavy_slots,
            light_slots_max=config.light_slots,
        )
        self.managers: dict[str, ProcessManager] = {}
        self._running = True
        self._app_tasks: dict[str, asyncio.Task] = {}
        self._commands_dir = Path(__file__).parent.parent / "commands"
        self._commands_dir.mkdir(exist_ok=True)

    def _get_semaphore(self, slot: str) -> asyncio.Semaphore | None:
        if slot == "heavy":
            return self.heavy_sem
        elif slot == "light":
            return self.light_sem
        return None  # always — sem semáforo

    def _init_app_states(self) -> None:
        for name, cfg in self.config.apps.items():
            self.state.apps[name] = AppState(name=name, slot=cfg.slot)

    def _parse_schedule(self, schedule: str) -> CronTrigger | IntervalTrigger | None:
        """Converte string de schedule para trigger do APScheduler."""
        cron_match = re.match(r"cron\((.+)\)", schedule)
        if cron_match:
            params = {}
            for part in cron_match.group(1).split(","):
                key, val = part.strip().split("=")
                params[key.strip()] = val.strip()
            return CronTrigger(**params)

        interval_match = re.match(r"interval\((.+)\)", schedule)
        if interval_match:
            params = {}
            for part in interval_match.group(1).split(","):
                key, val = part.strip().split("=")
                params[key.strip()] = int(val.strip())
            return IntervalTrigger(**params)

        return None

    # ── Execução de jobs ────────────────────────────────────────

    async def _run_job(self, app_name: str) -> None:
        """Executa um job batch com controle de semáforo."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]
        sem = self._get_semaphore(cfg.slot)

        if not app_state.enabled:
            return

        if app_state.status == "running":
            logger.debug(f"[{app_name}] Já está rodando, pulando execução")
            return

        app_state.status = "queued"
        save_state(self.state)

        if sem:
            logger.debug(f"[{app_name}] Aguardando slot {cfg.slot}...")
            await sem.acquire()
            self._update_slot_counts()

        try:
            manager = ProcessManager(cfg, app_state, self.alerter)
            self.managers[app_name] = manager
            pid = await manager.start()
            if pid:
                save_state(self.state)
                await manager.wait_with_monitoring()
        finally:
            if sem:
                sem.release()
                self._update_slot_counts()
            save_state(self.state)

    async def _run_loop_job(self, app_name: str) -> None:
        """Executa um job em loop contínuo com pausa entre ciclos."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]
        while self._running and app_state.enabled:
            await self._run_job(app_name)
            if not app_state.enabled:
                break
            if cfg.pause_between > 0:
                app_state.status = "off"
                app_state.next_run = f"pausa {cfg.pause_between}s"
                save_state(self.state)
                # Sleep interruptível para responder a stop/pause rápido
                for _ in range(cfg.pause_between):
                    if not app_state.enabled or not self._running:
                        break
                    await asyncio.sleep(1)

    async def _run_always_service(self, app_name: str) -> None:
        """Mantém um serviço always-on com auto-restart."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]

        while self._running and app_state.enabled:
            manager = ProcessManager(cfg, app_state, self.alerter)
            self.managers[app_name] = manager
            pid = await manager.start()
            if pid is None:
                logger.error(f"[{app_name}] Falha ao iniciar serviço, retry em 30s")
                for _ in range(30):
                    if not app_state.enabled or not self._running:
                        return
                    await asyncio.sleep(1)
                continue

            save_state(self.state)
            await manager.wait_with_monitoring()

            if not self._running or not app_state.enabled:
                break

            if cfg.restart_on_crash:
                logger.warning(f"[{app_name}] Serviço morreu, reiniciando em 5s...")
                await self.alerter.alert_crash_restart(app_name)
                await asyncio.sleep(5)
            else:
                app_state.status = "off"
                save_state(self.state)
                break

    # ── Controle de apps (start/stop/pause/resume) ──────────────

    def _enable_app(self, app_name: str) -> None:
        """Ativa um app — inicia task ou agenda no scheduler."""
        if app_name not in self.config.apps:
            logger.warning(f"[{app_name}] App não encontrado no config")
            return

        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]

        if app_state.enabled:
            logger.debug(f"[{app_name}] Já está ativo")
            return

        app_state.enabled = True
        logger.info(f"[{app_name}] Ativado pelo usuário")

        if cfg.slot == "always":
            task = asyncio.create_task(
                self._run_always_service(app_name), name=f"always:{app_name}"
            )
            self._app_tasks[app_name] = task
        elif cfg.schedule == "loop":
            task = asyncio.create_task(
                self._run_loop_job(app_name), name=f"loop:{app_name}"
            )
            self._app_tasks[app_name] = task
        elif cfg.schedule == "manual":
            # Manual: executa uma vez agora
            task = asyncio.create_task(
                self._run_job(app_name), name=f"manual:{app_name}"
            )
            self._app_tasks[app_name] = task
        else:
            # cron/interval: agenda no scheduler
            trigger = self._parse_schedule(cfg.schedule)
            if trigger:
                self.scheduler.add_job(
                    self._run_job,
                    trigger=trigger,
                    args=[app_name],
                    id=app_name,
                    name=app_name,
                    max_instances=1,
                    misfire_grace_time=60,
                    replace_existing=True,
                )
                logger.info(f"[{app_name}] Agendado: {cfg.schedule}")

        save_state(self.state)

    def _disable_app(self, app_name: str) -> None:
        """Desativa um app — mata o processo e remove do scheduler."""
        if app_name not in self.state.apps:
            return

        app_state = self.state.apps[app_name]
        app_state.enabled = False
        logger.info(f"[{app_name}] Desativado pelo usuário")

        # Matar processo se rodando
        if app_name in self.managers and self.managers[app_name].is_alive():
            self.managers[app_name].kill()

        # Cancelar task async
        if app_name in self._app_tasks:
            task = self._app_tasks[app_name]
            if not task.done():
                task.cancel()
            del self._app_tasks[app_name]

        # Remover do scheduler
        try:
            self.scheduler.remove_job(app_name)
        except Exception:
            pass

        app_state.status = "off"
        app_state.pid = None
        app_state.ram_mb = 0
        app_state.cpu_pct = 0
        app_state.next_run = ""
        save_state(self.state)

    def _pause_app(self, app_name: str) -> None:
        """Pausa um app — não mata o processo rodando, mas impede novas execuções."""
        if app_name not in self.state.apps:
            return

        app_state = self.state.apps[app_name]
        app_state.enabled = False
        logger.info(f"[{app_name}] Pausado pelo usuário")

        # Pausar no scheduler (não remove, só pausa)
        try:
            self.scheduler.pause_job(app_name)
        except Exception:
            pass

        # Se não está rodando agora, marcar como paused
        if app_state.status != "running":
            app_state.status = "paused"
            # Cancelar task de loop/always se não estiver no meio de execução
            if app_name in self._app_tasks:
                task = self._app_tasks[app_name]
                if not task.done():
                    task.cancel()
                del self._app_tasks[app_name]
        else:
            # Está rodando — marcar que ao terminar vai pausar
            app_state.next_run = "pausado"

        save_state(self.state)

    def _resume_app(self, app_name: str) -> None:
        """Retoma um app pausado."""
        if app_name not in self.state.apps:
            return

        app_state = self.state.apps[app_name]
        if app_state.status not in ("paused", "off", "done", "failed", "timeout"):
            return

        logger.info(f"[{app_name}] Retomado pelo usuário")
        self._enable_app(app_name)

    def _start_all(self) -> None:
        """Ativa todos os apps."""
        logger.info("START ALL — ativando todos os apps")
        for name in self.config.apps:
            if not self.state.apps[name].enabled:
                self._enable_app(name)

    def _stop_all(self) -> None:
        """Desativa todos os apps."""
        logger.info("STOP ALL — desativando todos os apps")
        for name in list(self.state.apps.keys()):
            if self.state.apps[name].enabled or self.state.apps[name].status == "running":
                self._disable_app(name)

    # ── Monitoramento ───────────────────────────────────────────

    def _update_slot_counts(self) -> None:
        heavy_max = self.config.heavy_slots
        light_max = self.config.light_slots
        self.state.heavy_slots_used = heavy_max - self.heavy_sem._value
        self.state.light_slots_used = light_max - self.light_sem._value

    async def _monitor_loop(self) -> None:
        """Loop de monitoramento: atualiza métricas e processa comandos."""
        while self._running:
            # Métricas de sistema
            sys_metrics = get_system_metrics()
            self.state.total_ram_mb = sys_metrics["used_ram_mb"]
            self.state.total_cpu_pct = sys_metrics["cpu_pct"]

            # Métricas por processo ativo
            for name, manager in self.managers.items():
                if manager.is_alive():
                    from .resource_monitor import get_process_metrics
                    m = get_process_metrics(self.state.apps[name].pid)
                    self.state.apps[name].ram_mb = m.ram_mb
                    self.state.apps[name].cpu_pct = m.cpu_pct

            # Processar comandos da UI
            commands = read_commands(self._commands_dir)
            for cmd in commands:
                self._handle_command(cmd)

            # Atualizar next_run dos jobs agendados
            for job in self.scheduler.get_jobs():
                app_name = job.id
                if app_name in self.state.apps:
                    next_fire = job.next_run_time
                    if next_fire:
                        self.state.apps[app_name].next_run = next_fire.strftime("%H:%M:%S")

            self._update_slot_counts()
            save_state(self.state)
            await asyncio.sleep(5)

    def _handle_command(self, cmd: Command) -> None:
        """Processa um comando recebido da UI."""
        logger.info(f"Comando recebido: {cmd.action} {cmd.app_name}")
        if cmd.action == "start":
            self._enable_app(cmd.app_name)
        elif cmd.action == "stop":
            self._disable_app(cmd.app_name)
        elif cmd.action == "pause":
            self._pause_app(cmd.app_name)
        elif cmd.action == "resume":
            self._resume_app(cmd.app_name)
        elif cmd.action == "start_all":
            self._start_all()
        elif cmd.action == "stop_all":
            self._stop_all()

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Inicia o orquestrador em modo IDLE — apps só rodam quando ativados."""
        logger.info("=" * 60)
        logger.info("HIDRA CONTROL PLANE — Iniciando (modo idle)")
        logger.info(f"Apps configuradas: {len(self.config.apps)}")
        logger.info("=" * 60)

        self._init_app_states()
        self.scheduler.start()
        await self.alerter.alert_started()

        # Auto-start: ativar apenas apps marcados com auto_start: true
        auto_started = []
        for name, cfg in self.config.apps.items():
            if cfg.auto_start:
                self._enable_app(name)
                auto_started.append(name)

        if auto_started:
            logger.info(f"Auto-start: {', '.join(auto_started)}")
        else:
            logger.info("Nenhum app com auto_start — aguardando comandos da UI")

        save_state(self.state)
        logger.info("Orquestrador rodando. Use o dashboard para controlar os apps.")

        # Monitor loop roda indefinidamente
        await self._monitor_loop()

    async def stop(self) -> None:
        """Para o orquestrador gracefully."""
        logger.info("Parando orquestrador...")
        self._running = False
        self.scheduler.shutdown(wait=False)

        for name, manager in self.managers.items():
            if manager.is_alive():
                logger.info(f"[{name}] Parando...")
                manager.kill()

        for name, task in self._app_tasks.items():
            if not task.done():
                task.cancel()

        save_state(self.state)
        logger.info("Orquestrador parado.")
