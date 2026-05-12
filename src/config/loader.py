"""Carrega config.yaml e resolve variáveis de ambiente."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AppConfig:
    name: str
    slot: str  # "heavy", "light", "always"
    cwd: str
    cmd: str
    schedule: str = "manual"
    pause_between: int = 0
    max_ram_mb: int = 1024
    timeout: int = 600
    priority: int = 5
    restart_on_crash: bool = False
    auto_start: bool = False  # se True, inicia automaticamente ao subir o orchestrator
    gui: bool = False  # se True, app lança janela GUI — usa CREATE_BREAKAWAY_FROM_JOB
    env_file: str = ""  # caminho do .env a carregar antes de executar (opcional)
    env: dict[str, str] = field(default_factory=dict)
    # ── Pre-start hooks ─────────────────────────────────────
    # git_pull: DEPRECATED — desde a centralização do auto-update via
    # `settings.auto_update`, o orquestrador descobre o repo a partir do
    # cwd e atualiza automaticamente, sem flag por app. Mantido aqui só
    # para aceitar configs antigas sem quebrar; é ignorado em runtime.
    git_pull: bool = False
    # pre_start: lista de comandos shell executados sequencialmente no cwd
    # antes do app principal. Suporta substituição de {python} e {pip} pelo
    # executável do venv detectado em cwd/.venv/Scripts.
    pre_start: list[str] = field(default_factory=list)
    # Timeout TOTAL (segundos) de todos os pre_start somados
    pre_start_timeout: int = 300
    # Se True, falha em qualquer comando do pre_start aborta a execução do
    # app. Se False, apenas loga e segue.
    pre_start_required: bool = True
    # ── Cleanup pós-execução ────────────────────────────────
    # Lista de nomes de processos a matar após cada rodada do app (somada
    # à lista DEFAULT_ORPHAN_NAMES do módulo cleanup.py). Útil para apps
    # que lançam binários adicionais via shell intermediário e que costumam
    # escapar do Windows Job Object (ex: nodejs.exe, java.exe, edge.exe).
    # Comparação case-insensitive. Default vazio = só os defaults.
    kill_orphans: list[str] = field(default_factory=list)


@dataclass
class AlertsConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    on_failure: bool = True
    on_timeout: bool = True
    on_ram_exceeded: bool = True


@dataclass
class AutoUpdateConfig:
    """Configuração global do auto-update via git.

    O orquestrador descobre o repo a partir do `cwd` de cada app e faz
    `git fetch + git reset --hard <remote>/<branch>` antes de iniciar o
    processo (com slot e gate de memória já adquiridos). Não há
    configuração por app.
    """

    enabled: bool = True
    # Política em caso de falha do update (fetch/reset não completam OK):
    #   "abort_cycle"  → libera slot, alerta, próximo ciclo tenta de novo
    #   "skip_update"  → loga warning e segue rodando com a versão em disco
    on_failure: str = "abort_cycle"
    # Timeout total (s) para fetch + reset somados em cada app
    timeout_seconds: int = 60
    # Lista de paths absolutos de repo_root a IGNORAR (comparação por
    # path resolvido). Útil para repos em desenvolvimento ativo na VM
    # ou que tenham peculiaridades.
    skip_paths: list[str] = field(default_factory=list)


@dataclass
class ControlPlaneConfig:
    apps: dict[str, AppConfig]
    alerts: AlertsConfig
    auto_update: AutoUpdateConfig = field(default_factory=AutoUpdateConfig)
    log_dir: str = "logs/"
    log_rotation: str = "10 MB"
    log_retention: int = 30
    heavy_slots: int = 1
    light_slots: int = 3
    ram_safety_margin_mb: int = 512  # RAM reservada para o SO antes de liberar apps


ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value: str) -> str:
    """Substitui ${VAR} pelo valor da variável de ambiente."""
    if not isinstance(value, str):
        return value
    return ENV_VAR_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)), value
    )


def _resolve_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Resolve env vars recursivamente em um dict."""
    resolved = {}
    for k, v in d.items():
        if isinstance(v, str):
            resolved[k] = _resolve_env(v)
        elif isinstance(v, dict):
            resolved[k] = _resolve_dict(v)
        else:
            resolved[k] = v
    return resolved


def load_config(config_path: str | Path = "config.yaml") -> ControlPlaneConfig:
    """Carrega e valida o config.yaml."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config não encontrado: {path.resolve()}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _resolve_dict(raw)

    apps: dict[str, AppConfig] = {}
    for name, app_data in raw.get("apps", {}).items():
        apps[name] = AppConfig(
            name=name,
            slot=app_data.get("slot", "light"),
            cwd=app_data.get("cwd", "."),
            cmd=app_data.get("cmd", ""),
            schedule=app_data.get("schedule", "manual"),
            pause_between=app_data.get("pause_between", 0),
            max_ram_mb=app_data.get("max_ram_mb", 1024),
            timeout=app_data.get("timeout", 600),
            priority=app_data.get("priority", 5),
            restart_on_crash=app_data.get("restart_on_crash", False),
            auto_start=app_data.get("auto_start", False),
            gui=app_data.get("gui", False),
            env_file=app_data.get("env_file", ""),
            env=app_data.get("env", {}),
            git_pull=app_data.get("git_pull", False),
            pre_start=list(app_data.get("pre_start") or []),
            pre_start_timeout=app_data.get("pre_start_timeout", 300),
            pre_start_required=app_data.get("pre_start_required", True),
            kill_orphans=list(app_data.get("kill_orphans") or []),
        )

    alerts_data = raw.get("alerts", {})
    alerts = AlertsConfig(
        telegram_bot_token=alerts_data.get("telegram_bot_token", ""),
        telegram_chat_id=alerts_data.get("telegram_chat_id", ""),
        on_failure=alerts_data.get("on_failure", True),
        on_timeout=alerts_data.get("on_timeout", True),
        on_ram_exceeded=alerts_data.get("on_ram_exceeded", True),
    )

    settings = raw.get("settings", {})

    auto_update_raw = settings.get("auto_update", {}) or {}
    on_failure = auto_update_raw.get("on_failure", "abort_cycle")
    if on_failure not in ("abort_cycle", "skip_update"):
        on_failure = "abort_cycle"
    auto_update = AutoUpdateConfig(
        enabled=bool(auto_update_raw.get("enabled", True)),
        on_failure=on_failure,
        timeout_seconds=int(auto_update_raw.get("timeout_seconds", 60)),
        skip_paths=list(auto_update_raw.get("skip_paths") or []),
    )

    return ControlPlaneConfig(
        apps=apps,
        alerts=alerts,
        auto_update=auto_update,
        log_dir=settings.get("log_dir", "logs/"),
        log_rotation=settings.get("log_rotation", "10 MB"),
        log_retention=settings.get("log_retention", 30),
        heavy_slots=settings.get("heavy_slots", 1),
        light_slots=settings.get("light_slots", 3),
        ram_safety_margin_mb=settings.get("ram_safety_margin_mb", 512),
    )
