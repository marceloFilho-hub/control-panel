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


def build_schedule_string(schedule_type: str, **_kwargs: Any) -> str:
    """Constrói string de schedule a partir dos campos da UI.

    schedule_type: "manual" ou "loop".
    Não suportamos mais cron/interval — tempo entre rodagens vira pause_between.
    """
    if schedule_type == "loop":
        return "loop"
    return "manual"


def parse_schedule_string(schedule: str) -> tuple[str, dict[str, int]]:
    """Extrai tipo do schedule para preencher o formulário ao editar.

    Converte schedules legados (cron/interval) em "loop" com pause_between
    já que agora só lidamos com manual ou loop-com-pausa.
    """
    import re

    if schedule == "loop":
        return "loop", {}
    if schedule == "manual":
        return "manual", {}

    # Legado: interval(...) vira "loop" com pause_between equivalente
    interval_match = re.match(r"interval\((.+)\)", schedule)
    if interval_match:
        params = {}
        for part in interval_match.group(1).split(","):
            key, val = part.strip().split("=")
            params[key.strip()] = int(val.strip())
        seconds = (
            params.get("seconds", 0)
            + params.get("minutes", 0) * 60
            + params.get("hours", 0) * 3600
        )
        return "loop", {"pause_between": seconds}

    # Legado: cron(...) — não há equivalente direto; vira manual
    return "manual", {}
