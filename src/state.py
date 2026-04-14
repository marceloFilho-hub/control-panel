"""Estado compartilhado entre orchestrator e dashboard via JSON."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AppState:
    name: str
    slot: str
    status: str = "off"  # off, queued, running, done, failed, timeout, paused
    enabled: bool = False  # se o app foi ativado pelo usuário
    pid: Optional[int] = None
    ram_mb: float = 0.0
    cpu_pct: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    last_error: str = ""
    run_count: int = 0
    fail_count: int = 0
    next_run: str = ""
    last_duration_s: float = 0.0


@dataclass
class ControlPlaneState:
    apps: dict[str, AppState] = field(default_factory=dict)
    started_at: float = 0.0
    total_ram_mb: float = 0.0
    total_cpu_pct: float = 0.0
    heavy_slots_used: int = 0
    heavy_slots_max: int = 1
    light_slots_used: int = 0
    light_slots_max: int = 3


STATE_FILE = Path(__file__).parent.parent / "state.json"
_lock = threading.Lock()


def save_state(state: ControlPlaneState) -> None:
    """Salva o estado em state.json de forma thread-safe."""
    with _lock:
        data = asdict(state)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)


def load_state() -> ControlPlaneState:
    """Carrega o estado do state.json. Retorna vazio se não existir."""
    if not STATE_FILE.exists():
        return ControlPlaneState()

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ControlPlaneState()

    apps = {}
    for name, app_data in raw.get("apps", {}).items():
        apps[name] = AppState(
            name=name,
            slot=app_data.get("slot", "light"),
            status=app_data.get("status", "off"),
            enabled=app_data.get("enabled", False),
            pid=app_data.get("pid"),
            ram_mb=app_data.get("ram_mb", 0.0),
            cpu_pct=app_data.get("cpu_pct", 0.0),
            started_at=app_data.get("started_at"),
            finished_at=app_data.get("finished_at"),
            last_exit_code=app_data.get("last_exit_code"),
            last_error=app_data.get("last_error", ""),
            run_count=app_data.get("run_count", 0),
            fail_count=app_data.get("fail_count", 0),
            next_run=app_data.get("next_run", ""),
            last_duration_s=app_data.get("last_duration_s", 0.0),
        )

    return ControlPlaneState(
        apps=apps,
        started_at=raw.get("started_at", 0.0),
        total_ram_mb=raw.get("total_ram_mb", 0.0),
        total_cpu_pct=raw.get("total_cpu_pct", 0.0),
        heavy_slots_used=raw.get("heavy_slots_used", 0),
        heavy_slots_max=raw.get("heavy_slots_max", 1),
        light_slots_used=raw.get("light_slots_used", 0),
        light_slots_max=raw.get("light_slots_max", 3),
    )


@dataclass
class Command:
    action: str  # start, stop, pause, resume, start_all, stop_all
    app_name: str = ""  # vazio para comandos globais


def write_command(commands_dir: Path, action: str, app_name: str = "") -> None:
    """Cria um arquivo .trigger com o comando desejado."""
    commands_dir.mkdir(exist_ok=True)
    filename = f"{action}_{app_name}.trigger" if app_name else f"{action}.trigger"
    (commands_dir / filename).touch()


def read_commands(commands_dir: Path | None = None) -> list[Command]:
    """Lê e remove arquivos .trigger do diretório de comandos."""
    d = commands_dir or (Path(__file__).parent.parent / "commands")
    commands: list[Command] = []
    if not d.exists():
        return commands
    for f in d.glob("*.trigger"):
        stem = f.stem
        # Parse: action_appname ou action (global)
        if stem in ("start_all", "stop_all"):
            commands.append(Command(action=stem))
        elif "_" in stem:
            action, app_name = stem.split("_", 1)
            if action in ("start", "stop", "pause", "resume", "run"):
                # "run" mantido para compatibilidade
                if action == "run":
                    action = "start"
                commands.append(Command(action=action, app_name=app_name))
        f.unlink()
    return commands
