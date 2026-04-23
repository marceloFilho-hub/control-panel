"""Orquestrador central — gerencia ciclo de vida dos apps.

Filosofia: o orquestrador NÃO cuida de horário (cron). Ele cuida de:

  1. Tempo entre rodagens (pause_between)      — apps efêmeros
  2. Fila por slot (heavy=1, light=3)           — controle de concorrência
  3. Fila por memória disponível                — antes de iniciar um app,
                                                  verifica se a RAM disponível
                                                  comporta o max_ram_mb dele
                                                  (menos a margem de segurança).
                                                  Se não comportar, aguarda.

Schedules válidos:
  "manual"  → roda uma vez ao clicar em Start
  "loop"    → roda uma vez, aguarda pause_between, repete (apps efêmeros)
  (always usa cfg.slot == "always" com restart_on_crash)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from loguru import logger

from ..config.loader import ControlPlaneConfig, load_config
from ..observability.alerter import TelegramAlerter
from ..process.manager import ProcessManager
from ..process.resource_monitor import get_system_metrics
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
        self.heavy_sem = asyncio.Semaphore(config.heavy_slots)
        self.light_sem = asyncio.Semaphore(config.light_slots)
        self.state = ControlPlaneState(
            started_at=time.time(),
            heavy_slots_max=config.heavy_slots,
            light_slots_max=config.light_slots,
            ram_safety_margin_mb=config.ram_safety_margin_mb,
        )
        self.managers: dict[str, ProcessManager] = {}
        self._running = True
        self._app_tasks: dict[str, asyncio.Task] = {}
        self._commands_dir = Path(__file__).parent.parent / "commands"
        self._commands_dir.mkdir(exist_ok=True)
        # Filas rastreáveis
        self._heavy_queue: list[str] = []
        self._light_queue: list[str] = []
        self._memory_queue: list[str] = []
        # Evento sinalizado quando RAM libera — desperta quem aguarda memória
        self._memory_event = asyncio.Event()
        # Hot reload do config.yaml
        self._config_path = Path(__file__).parent.parent / "config.yaml"
        self._last_config_mtime = (
            self._config_path.stat().st_mtime if self._config_path.exists() else 0.0
        )

    # ── Semáforos e filas ───────────────────────────────────────

    def _get_semaphore(self, slot: str) -> asyncio.Semaphore | None:
        if slot == "heavy":
            return self.heavy_sem
        elif slot == "light":
            return self.light_sem
        return None

    def _get_queue(self, slot: str) -> list[str] | None:
        if slot == "heavy":
            return self._heavy_queue
        elif slot == "light":
            return self._light_queue
        return None

    # ── Memory-aware scheduling ─────────────────────────────────

    def _get_available_ram_mb(self) -> float:
        """RAM disponível no sistema (já descontada a margem de segurança)."""
        metrics = get_system_metrics()
        raw_available = metrics["available_ram_mb"]
        self.state.available_ram_mb = raw_available
        return max(0.0, raw_available - self.config.ram_safety_margin_mb)

    async def _wait_for_memory(self, app_name: str, required_mb: int) -> None:
        """Espera até que haja RAM suficiente para iniciar o app.

        Adiciona o app à memory_queue para visibilidade no dashboard.
        Re-checa a cada 5s OU quando outro app liberar RAM (evento).
        """
        app_state = self.state.apps[app_name]

        while self._running and app_state.enabled:
            available = self._get_available_ram_mb()
            if available >= required_mb:
                return

            if app_name not in self._memory_queue:
                self._memory_queue.append(app_name)
                app_state.next_run = (
                    f"aguardando RAM ({available:.0f}/{required_mb} MB)"
                )
                logger.info(
                    f"[{app_name}] RAM insuficiente: {available:.0f} MB disponível, "
                    f"precisa de {required_mb} MB — entrou na fila de memória"
                )
                save_state(self.state)

            # Aguarda sinal OU 5s
            try:
                await asyncio.wait_for(self._memory_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            # Reset do evento (outros aguardadores também reavaliarão)
            self._memory_event.clear()

        # Sair da fila ao cancelar ou desativar
        if app_name in self._memory_queue:
            self._memory_queue.remove(app_name)

    def _release_memory_event(self) -> None:
        """Acorda quem está aguardando RAM liberar."""
        self._memory_event.set()

    # ── Execução de jobs ────────────────────────────────────────

    async def _run_job(self, app_name: str) -> None:
        """Executa um job com controle de semáforo + check de memória."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]
        sem = self._get_semaphore(cfg.slot)
        queue = self._get_queue(cfg.slot)

        if not app_state.enabled:
            return

        if app_state.status == "running":
            logger.debug(f"[{app_name}] Já está rodando, pulando execução")
            return

        app_state.status = "queued"
        if queue is not None and app_name not in queue:
            queue.append(app_name)
        save_state(self.state)

        # 1) Aguardar slot (semáforo FIFO)
        if sem:
            await sem.acquire()
            if queue is not None and app_name in queue:
                queue.remove(app_name)
            self._update_slot_counts()

        try:
            # 2) Aguardar RAM disponível
            await self._wait_for_memory(app_name, cfg.max_ram_mb)

            if not app_state.enabled or not self._running:
                return

            # 3) Iniciar processo
            manager = ProcessManager(cfg, app_state, self.alerter)
            self.managers[app_name] = manager
            pid = await manager.start()
            if pid:
                # Remover da fila de memória se ainda estava
                if app_name in self._memory_queue:
                    self._memory_queue.remove(app_name)
                save_state(self.state)
                await manager.wait_with_monitoring()
        finally:
            if sem:
                sem.release()
                self._update_slot_counts()
            # Liberou RAM — acordar quem aguarda
            self._release_memory_event()
            save_state(self.state)

    async def _run_loop_job(self, app_name: str) -> None:
        """Loop: roda job efêmero, aguarda pause_between, repete."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]
        while self._running and app_state.enabled:
            await self._run_job(app_name)
            if not app_state.enabled:
                break
            if cfg.pause_between > 0:
                app_state.status = "off"
                app_state.next_run = f"próxima em {cfg.pause_between}s"
                save_state(self.state)
                for _ in range(cfg.pause_between):
                    if not app_state.enabled or not self._running:
                        break
                    await asyncio.sleep(1)

    async def _run_always_service(self, app_name: str) -> None:
        """Mantém um serviço always-on com auto-restart."""
        cfg = self.config.apps[app_name]
        app_state = self.state.apps[app_name]

        while self._running and app_state.enabled:
            # Check de memória antes de iniciar serviço always
            await self._wait_for_memory(app_name, cfg.max_ram_mb)
            if not app_state.enabled or not self._running:
                return

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
            self._release_memory_event()

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
        """Ativa um app e inicia sua task de execução."""
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
        elif cfg.schedule == "loop":
            task = asyncio.create_task(
                self._run_loop_job(app_name), name=f"loop:{app_name}"
            )
        else:
            # manual — roda uma vez
            task = asyncio.create_task(
                self._run_job(app_name), name=f"manual:{app_name}"
            )
        self._app_tasks[app_name] = task
        save_state(self.state)

    def _disable_app(self, app_name: str) -> None:
        """Desativa um app — mata o processo e cancela a task."""
        if app_name not in self.state.apps:
            return

        app_state = self.state.apps[app_name]
        app_state.enabled = False
        logger.info(f"[{app_name}] Desativado pelo usuário")

        if app_name in self.managers and self.managers[app_name].is_alive():
            self.managers[app_name].kill()

        if app_name in self._app_tasks:
            task = self._app_tasks[app_name]
            if not task.done():
                task.cancel()
            del self._app_tasks[app_name]

        # Limpar das filas
        for q in (self._heavy_queue, self._light_queue, self._memory_queue):
            if app_name in q:
                q.remove(app_name)

        app_state.status = "off"
        app_state.pid = None
        app_state.ram_mb = 0
        app_state.cpu_pct = 0
        app_state.next_run = ""
        self._release_memory_event()  # pode ter liberado slot pra outro
        save_state(self.state)

    def _pause_app(self, app_name: str) -> None:
        """Pausa — não mata o processo rodando, mas impede novas execuções."""
        if app_name not in self.state.apps:
            return

        app_state = self.state.apps[app_name]
        app_state.enabled = False
        logger.info(f"[{app_name}] Pausado pelo usuário")

        if app_state.status != "running":
            app_state.status = "paused"
            if app_name in self._app_tasks:
                task = self._app_tasks[app_name]
                if not task.done():
                    task.cancel()
                del self._app_tasks[app_name]
        else:
            app_state.next_run = "pausado após término"

        save_state(self.state)

    def _resume_app(self, app_name: str) -> None:
        if app_name not in self.state.apps:
            return
        app_state = self.state.apps[app_name]
        if app_state.status not in ("paused", "off", "done", "failed", "timeout"):
            return
        logger.info(f"[{app_name}] Retomado pelo usuário")
        self._enable_app(app_name)

    def _start_all(self) -> None:
        logger.info("START ALL — ativando todos os apps")
        for name in self.config.apps:
            if not self.state.apps[name].enabled:
                self._enable_app(name)

    def _stop_all(self) -> None:
        logger.info("STOP ALL — desativando todos os apps")
        for name in list(self.state.apps.keys()):
            if self.state.apps[name].enabled or self.state.apps[name].status == "running":
                self._disable_app(name)

    def _init_app_states(self) -> None:
        for name, cfg in self.config.apps.items():
            self.state.apps[name] = AppState(name=name, slot=cfg.slot)

    # ── Monitoramento ───────────────────────────────────────────

    def _update_slot_counts(self) -> None:
        heavy_max = self.config.heavy_slots
        light_max = self.config.light_slots
        self.state.heavy_slots_used = heavy_max - self.heavy_sem._value
        self.state.light_slots_used = light_max - self.light_sem._value
        self.state.heavy_queue = list(self._heavy_queue)
        self.state.light_queue = list(self._light_queue)
        self.state.memory_queue = list(self._memory_queue)

    # ── Hot reload do config.yaml ───────────────────────────────

    def _check_config_changed(self) -> bool:
        if not self._config_path.exists():
            return False
        current_mtime = self._config_path.stat().st_mtime
        if current_mtime > self._last_config_mtime:
            self._last_config_mtime = current_mtime
            self.state.config_mtime = current_mtime
            return True
        return False

    def _reload_config(self) -> None:
        try:
            new_config = load_config(self._config_path)
        except Exception as e:
            logger.error(f"Falha ao recarregar config.yaml: {e}")
            return

        old_apps = set(self.config.apps.keys())
        new_apps = set(new_config.apps.keys())
        added = new_apps - old_apps
        removed = old_apps - new_apps
        common = old_apps & new_apps

        for name in removed:
            logger.info(f"[{name}] Removido do config.yaml — desabilitando")
            if self.state.apps.get(name) and self.state.apps[name].enabled:
                self._disable_app(name)
            self.state.apps.pop(name, None)

        for name in added:
            cfg = new_config.apps[name]
            logger.info(
                f"[{name}] Novo app — slot={cfg.slot}, schedule={cfg.schedule}"
            )
            self.state.apps[name] = AppState(name=name, slot=cfg.slot)

        changed = []
        for name in common:
            old_cfg = self.config.apps[name]
            new_cfg = new_config.apps[name]
            if (
                old_cfg.cmd != new_cfg.cmd
                or old_cfg.cwd != new_cfg.cwd
                or old_cfg.schedule != new_cfg.schedule
                or old_cfg.slot != new_cfg.slot
                or old_cfg.pause_between != new_cfg.pause_between
                or old_cfg.max_ram_mb != new_cfg.max_ram_mb
                or old_cfg.timeout != new_cfg.timeout
            ):
                changed.append(name)

        self.config = new_config
        self.state.ram_safety_margin_mb = new_config.ram_safety_margin_mb

        for name in changed:
            was_enabled = self.state.apps[name].enabled
            logger.info(f"[{name}] Config alterado — reaplicando (enabled={was_enabled})")
            self.state.apps[name].slot = new_config.apps[name].slot
            if was_enabled:
                self._disable_app(name)
                self._enable_app(name)

        save_state(self.state)
        logger.info(
            f"Config recarregado: +{len(added)} -{len(removed)} ~{len(changed)}"
        )

    async def _monitor_loop(self) -> None:
        """Loop de monitoramento: métricas, hot reload, comandos."""
        while self._running:
            # Métricas de sistema
            sys_metrics = get_system_metrics()
            self.state.total_ram_mb = sys_metrics["used_ram_mb"]
            self.state.total_cpu_pct = sys_metrics["cpu_pct"]
            self.state.available_ram_mb = sys_metrics["available_ram_mb"]

            # Métricas por processo ativo
            for name, manager in self.managers.items():
                if manager.is_alive():
                    from ..process.resource_monitor import get_process_metrics
                    m = get_process_metrics(self.state.apps[name].pid)
                    self.state.apps[name].ram_mb = m.ram_mb
                    self.state.apps[name].cpu_pct = m.cpu_pct

            # Hot reload
            if self._check_config_changed():
                logger.info("Mudança detectada em config.yaml — recarregando")
                self._reload_config()

            # Comandos
            commands = read_commands(self._commands_dir)
            for cmd in commands:
                self._handle_command(cmd)

            # Acordar quem aguarda RAM (pode ter liberado passivamente)
            if self._memory_queue:
                self._release_memory_event()

            self._update_slot_counts()
            save_state(self.state)
            await asyncio.sleep(5)

    def _handle_command(self, cmd: Command) -> None:
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
        elif cmd.action == "reload":
            logger.info("Reload manual do config.yaml solicitado")
            self._reload_config()

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("=" * 60)
        logger.info("HIDRA CONTROL PLANE — Iniciando (modo idle)")
        logger.info(f"RAM safety margin: {self.config.ram_safety_margin_mb} MB")
        logger.info("=" * 60)

        logger.info(f"Apps no config: {len(self.config.apps)}")

        self._init_app_states()
        await self.alerter.alert_started()

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

        await self._monitor_loop()

    async def stop(self) -> None:
        logger.info("Parando orquestrador...")
        self._running = False

        for name, manager in self.managers.items():
            if manager.is_alive():
                logger.info(f"[{name}] Parando...")
                manager.kill()

        for name, task in self._app_tasks.items():
            if not task.done():
                task.cancel()

        save_state(self.state)
        logger.info("Orquestrador parado.")
