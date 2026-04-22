"""Descobre apps na pasta 'apps_executaveis/' e sincroniza com config.yaml.

A pasta é a fonte primária da verdade — qualquer arquivo executável (.vbs,
.exe, .bat, .ps1, .py, .lnk) que aparece lá vira automaticamente uma entrada
no dashboard. Os settings (slot, pause_between, max_ram_mb, etc.) são
persistidos em config.yaml e mantidos quando o arquivo continua existindo.

Apps removidos da pasta são limpos do config.yaml automaticamente.
Apps em config.yaml com cwd/cmd absolutos (não vindos da pasta) são
preservados para compatibilidade com o modelo antigo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from .executable_detector import ExecutableInfo, build_command, detect
except ImportError:
    # Permite rodar como script (streamlit importa dashboard.py diretamente)
    from executable_detector import ExecutableInfo, build_command, detect  # type: ignore

APPS_DIR = Path(__file__).parent.parent / "apps_executaveis"
APPS_DIR.mkdir(exist_ok=True)

SUPPORTED_EXTENSIONS = {".vbs", ".exe", ".bat", ".cmd", ".ps1", ".py", ".lnk"}

# Defaults para apps recém-descobertos (não tocam apps já em config.yaml)
DEFAULT_SLOT = "light"
DEFAULT_PAUSE_BETWEEN = 600  # 10 min
DEFAULT_MAX_RAM_MB = 512
DEFAULT_TIMEOUT = 600


@dataclass
class DiscoveredApp:
    """Representa um arquivo executável encontrado na pasta."""
    name: str               # nome do arquivo sem extensão (id no config)
    file_path: Path         # caminho absoluto do arquivo
    info: ExecutableInfo    # tipo detectado, cwd, etc.

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def kind(self) -> str:
        return self.info.kind

    @property
    def icon(self) -> str:
        return self.info.icon


def scan_apps_dir() -> list[DiscoveredApp]:
    """Escaneia a pasta de apps e retorna os arquivos executáveis encontrados."""
    if not APPS_DIR.exists():
        return []

    apps: list[DiscoveredApp] = []
    for f in sorted(APPS_DIR.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if f.name.startswith("."):
            continue  # .gitkeep, .gitignore, etc.

        info = detect(str(f))
        # ID = nome do arquivo sem extensão, sanitizado
        app_id = f.stem.lower().replace(" ", "_").replace("-", "_")
        apps.append(DiscoveredApp(name=app_id, file_path=f, info=info))

    return apps


def is_managed_by_pasta(app_data: dict) -> bool:
    """Detecta se uma entrada do config.yaml veio da pasta de executáveis."""
    cwd = str(app_data.get("cwd", ""))
    return APPS_DIR.name in cwd.replace("\\", "/")


def sync_config_with_pasta(raw_config: dict) -> tuple[dict, list[str], list[str]]:
    """Sincroniza o config.yaml bruto com os arquivos da pasta.

    - Adiciona apps novos (com defaults) ao config se ainda não existirem
    - Remove apps que sumiram da pasta (apenas os marcados como managed)
    - Preserva os settings de apps já cadastrados (não sobrescreve pause_between, etc.)
    - Preserva apps com cwd absoluto fora da pasta (modo legado)

    Retorna (config_atualizado, adicionados, removidos).
    """
    apps = raw_config.setdefault("apps", {})
    discovered = {a.name: a for a in scan_apps_dir()}

    added: list[str] = []
    removed: list[str] = []

    # 1) Adicionar novos
    for name, app in discovered.items():
        if name in apps:
            # Já existe — apenas atualizar cmd/cwd se o arquivo mudou de tipo/local
            existing = apps[name]
            new_cmd, new_cwd = build_command(str(app.file_path))
            if existing.get("cmd") != new_cmd or existing.get("cwd") != new_cwd:
                existing["cmd"] = new_cmd
                existing["cwd"] = new_cwd
            continue

        cmd, cwd = build_command(str(app.file_path))
        apps[name] = {
            "slot": DEFAULT_SLOT,
            "cwd": cwd,
            "cmd": cmd,
            "schedule": "manual",  # começa desativado/manual
            "max_ram_mb": DEFAULT_MAX_RAM_MB,
            "timeout": DEFAULT_TIMEOUT,
            "_source": "pasta",  # marca pra distinguir do legado
        }
        added.append(name)

    # 2) Remover apps que sumiram da pasta (só se vieram da pasta)
    for name in list(apps.keys()):
        app_data = apps[name]
        came_from_pasta = (
            app_data.get("_source") == "pasta"
            or is_managed_by_pasta(app_data)
        )
        if came_from_pasta and name not in discovered:
            del apps[name]
            removed.append(name)

    return raw_config, added, removed


def get_default_app_data(file_path: Path) -> dict:
    """Defaults para um app recém-descoberto."""
    cmd, cwd = build_command(str(file_path))
    return {
        "slot": DEFAULT_SLOT,
        "cwd": cwd,
        "cmd": cmd,
        "schedule": "manual",
        "max_ram_mb": DEFAULT_MAX_RAM_MB,
        "timeout": DEFAULT_TIMEOUT,
        "_source": "pasta",
    }
