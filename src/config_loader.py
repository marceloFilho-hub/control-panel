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
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class AlertsConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    on_failure: bool = True
    on_timeout: bool = True
    on_ram_exceeded: bool = True


@dataclass
class ControlPlaneConfig:
    apps: dict[str, AppConfig]
    alerts: AlertsConfig
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
            env=app_data.get("env", {}),
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

    return ControlPlaneConfig(
        apps=apps,
        alerts=alerts,
        log_dir=settings.get("log_dir", "logs/"),
        log_rotation=settings.get("log_rotation", "10 MB"),
        log_retention=settings.get("log_retention", 30),
        heavy_slots=settings.get("heavy_slots", 1),
        light_slots=settings.get("light_slots", 3),
        ram_safety_margin_mb=settings.get("ram_safety_margin_mb", 512),
    )
