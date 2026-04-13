"""Orquestrador central — scheduler + semáforos + ciclo de vida."""

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
        # cron(hour=7, minute=0)
        cron_match = re.match(r"cron\((.+)\)", schedule)
        if cron_match:
            params = {}
            for part in cron_match.group(1).split(","):
                key, val = part.strip().split("=")
                params[key.strip()] = val.strip()
            return CronTrigger(**params)

        # interval(minutes=15)
        interval_match = re.match(r"interval\((.+)\)", schedule)
        if interval_match:
            params = {}
            for part in interval_match.group(1).split(","):
                key, val = part.strip().split("=")
                params[key.strip()] = int(val.strip())
            return IntervalTrigger(**params)

        return None

    async def _run_job(self, app_name: str) -> None:
        """Executa um job batch com controle de semáforo."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]
        sem = self._get_semaphore(cfg.slot)

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
        while self._running:
            await self._run_job(app_name)
            if cfg.pause_between > 0:
                app_state = self.state.apps[app_name]
                app_state.status = "off"
                app_state.next_run = f"pausa {cfg.pause_between}s"
                save_state(self.state)
                await asyncio.sleep(cfg.pause_between)

    async def _run_always_service(self, app_name: str) -> None:
        """Mantém um serviço always-on com auto-restart."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]

        while self._running:
            manager = ProcessManager(cfg, app_state, self.alerter)
            self.managers[app_name] = manager
            pid = await manager.start()
            if pid is None:
                logger.error(f"[{app_name}] Falha ao iniciar serviço, retry em 30s")
                await asyncio.sleep(30)
                continue

            save_state(self.state)
            await manager.wait_with_monitoring()

            if not self._running:
                break

            if cfg.restart_on_crash:
                logger.warning(f"[{app_name}] Serviço morreu, reiniciando em 5s...")
                await self.alerter.alert_crash_restart(app_name)
                await asyncio.sleep(5)
            else:
                app_state.status = "off"
                save_state(self.state)
                break

    def _update_slot_counts(self) -> None:
        """Atualiza contadores de slots usados no estado."""
        heavy_max = self.config.heavy_slots
        light_max = self.config.light_slots
        # Semaphore._value dá quantos slots estão LIVRES
        self.state.heavy_slots_used = heavy_max - self.heavy_sem._value
        self.state.light_slots_used = light_max - self.light_sem._value

    async def _monitor_loop(self) -> None:
        """Loop de monitoramento: atualiza métricas e checa comandos."""
        while self._running:
            # Atualizar métricas de sistema
            sys_metrics = get_system_metrics()
            self.state.total_ram_mb = sys_metrics["used_ram_mb"]
            self.state.total_cpu_pct = sys_metrics["cpu_pct"]

            # Atualizar métricas por processo ativo
            for name, manager in self.managers.items():
                if manager.is_alive():
                    from .resource_monitor import get_process_metrics
                    m = get_process_metrics(self.state.apps[name].pid)
                    self.state.apps[name].ram_mb = m.ram_mb
                    self.state.apps[name].cpu_pct = m.cpu_pct

            # Checar comandos manuais (triggers do dashboard)
            triggers = read_commands(self._commands_dir)
            for app_name in triggers:
                if app_name in self.config.apps:
                    logger.info(f"[{app_name}] Trigger manual recebido")
                    asyncio.create_task(self._run_job(app_name))
                elif app_name == "stop_all":
                    logger.info("Comando stop_all recebido")
                    await self.stop()

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

    async def start(self) -> None:
        """Inicia o orquestrador."""
        logger.info("=" * 60)
        logger.info("HIDRA CONTROL PLANE — Iniciando")
        logger.info(f"Apps configuradas: {len(self.config.apps)}")
        logger.info("=" * 60)

        self._init_app_states()
        await self.alerter.alert_started()

        tasks: list[asyncio.Task] = []

        for name, cfg in self.config.apps.items():
            if cfg.slot == "always":
                task = asyncio.create_task(
                    self._run_always_service(name), name=f"always:{name}"
                )
                tasks.append(task)
            elif cfg.schedule == "loop":
                task = asyncio.create_task(
                    self._run_loop_job(name), name=f"loop:{name}"
                )
                tasks.append(task)
            elif cfg.schedule != "manual":
                trigger = self._parse_schedule(cfg.schedule)
                if trigger:
                    self.scheduler.add_job(
                        self._run_job,
                        trigger=trigger,
                        args=[name],
                        id=name,
                        name=name,
                        max_instances=1,
                        misfire_grace_time=60,
                    )
                    logger.info(f"[{name}] Agendado: {cfg.schedule}")
                else:
                    logger.warning(f"[{name}] Schedule inválido: {cfg.schedule}")
            else:
                logger.info(f"[{name}] Manual — aguardando trigger")

        self.scheduler.start()
        monitor_task = asyncio.create_task(self._monitor_loop(), name="monitor")
        tasks.append(monitor_task)

        save_state(self.state)
        logger.info("Orquestrador rodando. Ctrl+C para parar.")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Tasks canceladas — encerrando")

    async def stop(self) -> None:
        """Para o orquestrador gracefully."""
        logger.info("Parando orquestrador...")
        self._running = False
        self.scheduler.shutdown(wait=False)

        for name, manager in self.managers.items():
            if manager.is_alive():
                logger.info(f"[{name}] Parando...")
                manager.kill()

        save_state(self.state)
        logger.info("Orquestrador parado.")
