"""Logger de execuções por app — stdout+stderr por run + histórico persistente.

Estrutura em disco:
  logs/
    {app_name}/
      {timestamp}_{exec_id}.log     # output bruto
      history.jsonl                  # 1 linha JSON por execução
      latest.log -> último .log      # (apenas referência; leitura usa history)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

LOGS_ROOT = Path(__file__).parent.parent / "logs"
MAX_HISTORY_LINES = 500  # mantém as últimas N execuções no history.jsonl


@dataclass
class ExecutionRecord:
    exec_id: str
    app_name: str
    started_at: float
    finished_at: float | None = None
    duration_s: float = 0.0
    exit_code: int | None = None
    status: str = "running"  # running, done, failed, timeout, killed
    error: str = ""
    pid: int | None = None
    log_file: str = ""
    peak_ram_mb: float = 0.0

    @property
    def started_at_str(self) -> str:
        return datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def finished_at_str(self) -> str:
        if not self.finished_at:
            return ""
        return datetime.fromtimestamp(self.finished_at).strftime("%Y-%m-%d %H:%M:%S")


class ExecutionLogger:
    """Gerencia o log de uma execução individual."""

    def __init__(self, app_name: str):
        self.app_name = app_name
        self.app_dir = LOGS_ROOT / app_name
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.app_dir / "history.jsonl"

        self.exec_id = uuid.uuid4().hex[:8]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.app_dir / f"{ts}_{self.exec_id}.log"
        self.record = ExecutionRecord(
            exec_id=self.exec_id,
            app_name=app_name,
            started_at=time.time(),
            log_file=str(self.log_file),
        )
        self._fh: IO | None = None

    def open(self) -> IO:
        """Abre o arquivo de log para escrita (modo append binário)."""
        if self._fh is None:
            self._fh = open(self.log_file, "ab", buffering=0)
            header = (
                f"=== {self.app_name} | exec_id={self.exec_id} | "
                f"{self.record.started_at_str} ===\n"
            ).encode("utf-8")
            self._fh.write(header)
        return self._fh

    def write(self, chunk: bytes) -> None:
        """Escreve bytes no log (stdout ou stderr do subprocesso)."""
        fh = self.open()
        try:
            fh.write(chunk)
        except Exception:
            pass

    def close(self, status: str, exit_code: int | None = None, error: str = "", peak_ram_mb: float = 0.0) -> None:
        """Fecha o log e persiste o registro no history.jsonl."""
        self.record.finished_at = time.time()
        self.record.duration_s = self.record.finished_at - self.record.started_at
        self.record.status = status
        self.record.exit_code = exit_code
        self.record.error = error[:500] if error else ""
        self.record.peak_ram_mb = peak_ram_mb

        if self._fh:
            try:
                footer = (
                    f"\n=== FIM | status={status} | exit={exit_code} | "
                    f"duration={self.record.duration_s:.1f}s ===\n"
                ).encode("utf-8")
                self._fh.write(footer)
                self._fh.close()
            except Exception:
                pass
            self._fh = None

        # Append no history.jsonl
        try:
            with open(self.history_file, "a", encoding="utf-8") as h:
                h.write(json.dumps(asdict(self.record), ensure_ascii=False) + "\n")
            self._trim_history()
        except Exception:
            pass

    def _trim_history(self) -> None:
        """Mantém apenas as últimas MAX_HISTORY_LINES entradas."""
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_HISTORY_LINES:
                trimmed = lines[-MAX_HISTORY_LINES:]
                self.history_file.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        except Exception:
            pass


# ── Leitura do histórico (usado pelo dashboard) ──────────────


def read_history(app_name: str, limit: int = 50) -> list[ExecutionRecord]:
    """Lê as últimas N execuções de um app."""
    history_file = LOGS_ROOT / app_name / "history.jsonl"
    if not history_file.exists():
        return []
    try:
        lines = history_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    records = []
    for line in lines[-limit:]:
        try:
            data = json.loads(line)
            records.append(ExecutionRecord(**data))
        except Exception:
            continue
    return list(reversed(records))  # mais recentes primeiro


def read_log_content(log_path: str, tail_kb: int = 64) -> str:
    """Lê o conteúdo de um arquivo de log. Limita a N KB do final."""
    p = Path(log_path)
    if not p.exists():
        return "(arquivo de log não encontrado)"
    try:
        size = p.stat().st_size
        if size <= tail_kb * 1024:
            return p.read_text(encoding="utf-8", errors="replace")
        with open(p, "rb") as f:
            f.seek(-tail_kb * 1024, 2)
            data = f.read()
        return f"... (truncado, mostrando últimos {tail_kb} KB)\n\n" + data.decode(
            "utf-8", errors="replace"
        )
    except Exception as e:
        return f"(erro ao ler log: {e})"


def list_apps_with_logs() -> list[str]:
    """Retorna nomes de apps que têm logs persistidos."""
    if not LOGS_ROOT.exists():
        return []
    return sorted([d.name for d in LOGS_ROOT.iterdir() if d.is_dir()])


def get_latest_log_path(app_name: str) -> str | None:
    """Retorna o caminho do log mais recente do app, se existir."""
    recs = read_history(app_name, limit=1)
    if recs and recs[0].log_file:
        return recs[0].log_file
    return None
