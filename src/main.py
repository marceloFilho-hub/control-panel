"""Entry point — inicia o orquestrador (modo idle) e o dashboard.

Apps NÃO iniciam automaticamente (exceto os marcados com auto_start: true).
O controle é feito pela interface web (dashboard) na porta 9000.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .config.loader import load_config
from .orchestration.orchestrator import Orchestrator

ROOT = Path(__file__).parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(log_dir: str, rotation: str, retention: int) -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> | "
        "{message}"
    ))
    logger.add(
        str(LOG_DIR / "hidra_control.log"),
        rotation=rotation,
        retention=f"{retention} days",
        level="DEBUG",
        encoding="utf-8",
    )


def start_dashboard() -> subprocess.Popen | None:
    """Inicia o dashboard Streamlit como processo separado."""
    dashboard_path = ROOT / "src" / "ui" / "dashboard.py"
    if not dashboard_path.exists():
        logger.warning("dashboard.py não encontrado, pulando")
        return None

    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(dashboard_path),
        "--server.port", "9000",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    logger.info(f"Iniciando dashboard na porta 9000")
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.error(f"Falha ao iniciar dashboard: {e}")
        return None


def _ensure_config_exists(config_path: Path) -> None:
    """No primeiro boot (VM, clone novo), cria config.yaml a partir do template.

    O config.yaml é ignorado pelo git (cada máquina tem o seu).
    O config.example.yaml é versionado e serve de base.
    """
    if config_path.exists():
        return
    example = config_path.parent / "config.example.yaml"
    if example.exists():
        config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info(f"config.yaml não existia — copiado de config.example.yaml")
    else:
        logger.warning(
            "config.yaml e config.example.yaml não existem — o orchestrator "
            "subirá com apps vazios"
        )


async def run() -> None:
    load_dotenv(ROOT / ".env")

    config_path = ROOT / "config.yaml"
    _ensure_config_exists(config_path)
    config = load_config(config_path)
    setup_logging(config.log_dir, config.log_rotation, config.log_retention)

    orchestrator = Orchestrator(config)

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def _shutdown(sig: int, frame) -> None:
        logger.info(f"Sinal {sig} recebido, encerrando...")
        asyncio.ensure_future(orchestrator.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Iniciar dashboard
    dash_proc = start_dashboard()

    try:
        await orchestrator.start()
    finally:
        if dash_proc:
            dash_proc.terminate()
            logger.info("Dashboard encerrado")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
