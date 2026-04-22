"""Escreve config.yaml a partir da UI — CRUD de apps sem editar manualmente."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HEADER = """\
# ═══════════════════════════════════════════════════════════════
# HIDRA CONTROL PLANE — Configuração de automações
# ═══════════════════════════════════════════════════════════════
#
# ARQUIVO GERENCIADO PELA UI
# Edite pelo dashboard em http://localhost:9000 → aba "Configurar".
# Edição manual é suportada, mas comentários inline serão perdidos
# no próximo save via UI.
#
# Slots:
#   heavy  → Semaphore(1) — máximo 1 job pesado por vez
#   light  → Semaphore(3) — até 3 jobs leves em paralelo
#   always → Serviço permanente com auto-restart
#
# Schedules:
#   "manual"                  → só roda via dashboard
#   "loop"                    → roda continuamente com pause_between
#   "cron(hour=7, minute=0)"  → cron-like (APScheduler)
#   "interval(minutes=15)"    → a cada N minutos/segundos
# ═══════════════════════════════════════════════════════════════

"""


def read_config_raw(config_path: Path) -> dict[str, Any]:
    """Lê o YAML bruto (sem resolver env vars)."""
    if not config_path.exists():
        return {"apps": {}, "alerts": {}, "settings": {}}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config_path: Path, data: dict[str, Any]) -> None:
    """Salva o config.yaml com header informativo."""
    tmp = config_path.with_suffix(".yaml.tmp")
    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    )
    tmp.write_text(HEADER + body, encoding="utf-8")
    tmp.replace(config_path)


def upsert_app(config_path: Path, app_name: str, app_data: dict[str, Any]) -> None:
    """Adiciona ou atualiza um app no config.yaml."""
    raw = read_config_raw(config_path)
    if "apps" not in raw:
        raw["apps"] = {}
    # Remover chaves com valor default/vazio para manter YAML limpo
    cleaned = {k: v for k, v in app_data.items() if v not in (None, "", 0, False)}
    # Preservar campos obrigatórios mesmo se "vazios"
    for required in ("slot", "cwd", "cmd"):
        if required in app_data and required not in cleaned:
            cleaned[required] = app_data[required]
    raw["apps"][app_name] = cleaned
    save_config(config_path, raw)


def delete_app(config_path: Path, app_name: str) -> bool:
    """Remove um app do config.yaml. Retorna True se removeu."""
    raw = read_config_raw(config_path)
    if "apps" in raw and app_name in raw["apps"]:
        del raw["apps"][app_name]
        save_config(config_path, raw)
        return True
    return False


def build_schedule_string(schedule_type: str, **kwargs: Any) -> str:
    """Constrói string de schedule a partir dos campos da UI.

    schedule_type: "manual", "loop", "cron_daily", "interval_minutes",
                   "interval_seconds", "interval_hours"
    """
    if schedule_type == "manual":
        return "manual"
    if schedule_type == "loop":
        return "loop"
    if schedule_type == "cron_daily":
        hour = kwargs.get("hour", 0)
        minute = kwargs.get("minute", 0)
        return f"cron(hour={hour}, minute={minute})"
    if schedule_type == "interval_minutes":
        n = kwargs.get("minutes", 15)
        return f"interval(minutes={n})"
    if schedule_type == "interval_seconds":
        n = kwargs.get("seconds", 60)
        return f"interval(seconds={n})"
    if schedule_type == "interval_hours":
        n = kwargs.get("hours", 1)
        return f"interval(hours={n})"
    return "manual"


def parse_schedule_string(schedule: str) -> tuple[str, dict[str, int]]:
    """Inverso de build_schedule_string — para preencher form ao editar."""
    import re

    if schedule == "manual":
        return "manual", {}
    if schedule == "loop":
        return "loop", {}

    cron_match = re.match(r"cron\((.+)\)", schedule)
    if cron_match:
        params = {}
        for part in cron_match.group(1).split(","):
            key, val = part.strip().split("=")
            params[key.strip()] = int(val.strip())
        return "cron_daily", params

    interval_match = re.match(r"interval\((.+)\)", schedule)
    if interval_match:
        params = {}
        for part in interval_match.group(1).split(","):
            key, val = part.strip().split("=")
            params[key.strip()] = int(val.strip())
        if "minutes" in params:
            return "interval_minutes", params
        if "seconds" in params:
            return "interval_seconds", params
        if "hours" in params:
            return "interval_hours", params

    return "manual", {}
