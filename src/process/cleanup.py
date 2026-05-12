"""Utilitários de limpeza pós-execução para manter a VM performática.

Filosofia: o orquestrador é dono da performance da VM. Após cada rodagem
de app — e também sob demanda via dashboard — limpamos:

  1. Processos órfãos por nome (chromedriver.exe, geckodriver.exe, ...)
     que escapam do Windows Job Object quando lançados via shell intermediário.
  2. `__pycache__/` recursivo dentro do cwd do app.
  3. Diretórios temporários do sistema (%TEMP%, %LOCALAPPDATA%\\Temp,
     C:\\Windows\\Temp) — só arquivos não bloqueados, sem mexer em
     standby memory (que exigiria privilégio elevado).
  4. Lixeira do Windows.

Todas as funções são best-effort: silenciam exceções (arquivo bloqueado,
permissão negada) e devolvem o quanto efetivamente foi liberado.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from loguru import logger


# Lista padrão de processos que costumam escapar do controle do orquestrador.
# Apps configuram complementos via `kill_orphans` no config.yaml.
DEFAULT_ORPHAN_NAMES: tuple[str, ...] = (
    "chromedriver.exe",
    "geckodriver.exe",
    "msedgedriver.exe",
    "iedriver.exe",
    "operadriver.exe",
    "playwright-headless-shell.exe",
)


@dataclass
class CleanupReport:
    """Resultado consolidado de uma rodada de cleanup."""

    orphans_killed: int = 0
    pycache_freed_mb: float = 0.0
    temp_freed_mb: float = 0.0
    recycle_bin_ok: bool = False
    ram_freed_mb: float = 0.0
    duration_s: float = 0.0
    details: list[str] = field(default_factory=list)

    @property
    def total_freed_mb(self) -> float:
        return self.pycache_freed_mb + self.temp_freed_mb

    def summary(self) -> str:
        parts = []
        if self.orphans_killed:
            parts.append(f"{self.orphans_killed} órfão(s)")
        if self.pycache_freed_mb > 0:
            parts.append(f"{self.pycache_freed_mb:.1f} MB __pycache__")
        if self.temp_freed_mb > 0:
            parts.append(f"{self.temp_freed_mb:.1f} MB temp")
        if self.recycle_bin_ok:
            parts.append("lixeira")
        if self.ram_freed_mb > 0:
            parts.append(f"~{self.ram_freed_mb:.0f} MB RAM")
        return ", ".join(parts) if parts else "nada a limpar"


# ── Kill por nome ─────────────────────────────────────────────────


def kill_orphans_by_name(
    names: list[str] | tuple[str, ...],
    exclude_pids: set[int] | None = None,
) -> int:
    """Mata todos os processos vivos cujo `name` está em `names`.

    Comparação case-insensitive. `exclude_pids` permite preservar PIDs
    específicos (ex: o próprio orquestrador, navegador interativo do usuário).
    Retorna a contagem de processos efetivamente mortos.
    """
    if not names:
        return 0
    targets = {n.lower() for n in names if n}
    exclude_pids = exclude_pids or set()
    killed = 0
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if pname not in targets:
                continue
            if proc.info["pid"] in exclude_pids:
                continue
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if killed:
        logger.info(f"[cleanup] {killed} processo(s) órfão(s) mortos: {sorted(targets)}")
    return killed


# ── __pycache__ ───────────────────────────────────────────────────


def purge_pycache(root: Path, max_depth: int = 8) -> float:
    """Apaga `__pycache__/` recursivamente dentro de `root`.

    Não desce em `.venv`, `node_modules`, `.git` — evita demora e remoção
    inadvertida de caches que pertencem a libs instaladas. `max_depth`
    limita a profundidade para não custar caro em árvores enormes.
    Retorna MB liberados (somatório do tamanho dos arquivos removidos).
    """
    if not root.exists() or not root.is_dir():
        return 0.0
    SKIP = {".venv", "node_modules", ".git", "site-packages", ".mypy_cache"}
    freed = 0
    for dirpath, dirnames, _ in os.walk(str(root)):
        # Limita profundidade
        rel_depth = Path(dirpath).relative_to(root).parts
        if len(rel_depth) > max_depth:
            dirnames[:] = []
            continue
        # Poda diretórios que não vale a pena descer
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if "__pycache__" in dirnames:
            target = Path(dirpath) / "__pycache__"
            try:
                size = _dir_size(target)
                shutil.rmtree(target, ignore_errors=True)
                if not target.exists():
                    freed += size
            except Exception:
                pass
            dirnames.remove("__pycache__")
    mb = freed / (1024 * 1024)
    if mb > 0.5:
        logger.info(f"[cleanup] __pycache__ em {root}: ~{mb:.1f} MB liberados")
    return mb


def _dir_size(path: Path) -> int:
    """Soma o tamanho de todos os arquivos em path (em bytes). Best-effort."""
    total = 0
    try:
        for entry in os.scandir(str(path)):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += _dir_size(Path(entry.path))
            except OSError:
                continue
    except OSError:
        pass
    return total


# ── Temp dirs ─────────────────────────────────────────────────────


def _windows_temp_dirs() -> list[Path]:
    """Retorna a lista de diretórios temporários a varrer."""
    candidates: list[Path] = []
    for env_var in ("TEMP", "TMP"):
        val = os.environ.get(env_var)
        if val:
            candidates.append(Path(val))
    # Pastas-padrão do Windows que nem sempre estão no ambiente
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Temp")
    win_dir = os.environ.get("SystemRoot", r"C:\Windows")
    candidates.append(Path(win_dir) / "Temp")
    # Dedup preservando ordem (caso TEMP==TMP, comum em VMs)
    seen: set[str] = set()
    result: list[Path] = []
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_dir():
            result.append(p)
    return result


def purge_temp_dirs(
    min_age_seconds: int = 300,
    dirs: list[Path] | None = None,
) -> float:
    """Apaga arquivos/pastas em diretórios temporários do Windows.

    Só remove itens com `mtime` mais antigo que `min_age_seconds` — assim
    não pisamos no que outro app pode estar usando AGORA. Arquivos em uso
    falham silenciosamente (Windows mantém handle exclusivo).

    Retorna o total em MB efetivamente removido.
    """
    targets = dirs if dirs is not None else _windows_temp_dirs()
    if not targets:
        return 0.0

    cutoff = time.time() - max(0, min_age_seconds)
    freed_bytes = 0

    for temp_dir in targets:
        try:
            for entry in os.scandir(str(temp_dir)):
                try:
                    st = entry.stat(follow_symlinks=False)
                    if st.st_mtime > cutoff:
                        continue
                    if entry.is_file(follow_symlinks=False):
                        size = st.st_size
                        try:
                            os.unlink(entry.path)
                            freed_bytes += size
                        except OSError:
                            pass
                    elif entry.is_dir(follow_symlinks=False):
                        size = _dir_size(Path(entry.path))
                        before = size
                        shutil.rmtree(entry.path, ignore_errors=True)
                        if not Path(entry.path).exists():
                            freed_bytes += before
                except OSError:
                    continue
        except OSError as e:
            logger.debug(f"[cleanup] Não consegui varrer {temp_dir}: {e}")

    mb = freed_bytes / (1024 * 1024)
    if mb > 0.5:
        logger.info(
            f"[cleanup] temp dirs ({len(targets)}): ~{mb:.1f} MB liberados"
        )
    return mb


# ── Lixeira ───────────────────────────────────────────────────────


def clear_recycle_bin() -> bool:
    """Esvazia a Lixeira do Windows via PowerShell.

    Best-effort: roda `Clear-RecycleBin -Force` em todas as drives. Retorna
    True se o comando completou (independente de ter havido conteúdo).
    """
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = result.returncode == 0
        if ok:
            logger.info("[cleanup] Lixeira do Windows esvaziada")
        else:
            logger.debug(
                f"[cleanup] Clear-RecycleBin rc={result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}"
            )
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"[cleanup] Falha no Clear-RecycleBin: {e}")
        return False


# ── Orquestração ──────────────────────────────────────────────────


def _available_ram_mb() -> float:
    return psutil.virtual_memory().available / (1024 * 1024)


def per_app_cleanup(
    app_name: str,
    cwd: Path,
    extra_orphans: list[str] | None = None,
    exclude_pids: set[int] | None = None,
) -> CleanupReport:
    """Cleanup leve disparado ao final de cada rodagem de um app.

    Mata órfãos default + extras do config + faz purge do `__pycache__` do
    cwd. Não toca em `%TEMP%` ou Lixeira — isso fica para o cleanup
    completo (sob demanda via dashboard) para não custar 1-3 s por rodagem.
    """
    start = time.time()
    ram_before = _available_ram_mb()
    report = CleanupReport()

    names = list(DEFAULT_ORPHAN_NAMES) + list(extra_orphans or [])
    report.orphans_killed = kill_orphans_by_name(names, exclude_pids=exclude_pids)
    report.pycache_freed_mb = purge_pycache(cwd)

    # GC do próprio orquestrador (libera buffers Python residuais)
    import gc
    gc.collect()

    report.ram_freed_mb = max(0.0, _available_ram_mb() - ram_before)
    report.duration_s = time.time() - start
    if report.orphans_killed or report.pycache_freed_mb > 0 or report.ram_freed_mb > 50:
        logger.info(
            f"[{app_name}] per_app_cleanup em {report.duration_s:.1f}s: {report.summary()}"
        )
    return report


def full_cleanup(
    app_cwds: list[Path],
    extra_orphans: list[str] | None = None,
    exclude_pids: set[int] | None = None,
) -> CleanupReport:
    """Cleanup completo (sob demanda via dashboard).

    Faz tudo o que o per_app faz, mais:
      - varre `__pycache__` em TODOS os cwds dos apps configurados
      - limpa `%TEMP%`, `%LOCALAPPDATA%\\Temp`, `C:\\Windows\\Temp`
      - esvazia a Lixeira do Windows
    """
    start = time.time()
    ram_before = _available_ram_mb()
    report = CleanupReport()

    names = list(DEFAULT_ORPHAN_NAMES) + list(extra_orphans or [])
    report.orphans_killed = kill_orphans_by_name(names, exclude_pids=exclude_pids)

    for cwd in app_cwds:
        try:
            report.pycache_freed_mb += purge_pycache(cwd)
        except Exception as e:
            logger.warning(f"[cleanup] purge_pycache falhou em {cwd}: {e}")

    report.temp_freed_mb = purge_temp_dirs()
    report.recycle_bin_ok = clear_recycle_bin()

    import gc
    gc.collect()

    report.ram_freed_mb = max(0.0, _available_ram_mb() - ram_before)
    report.duration_s = time.time() - start
    logger.info(
        f"[cleanup] full_cleanup em {report.duration_s:.1f}s: {report.summary()}"
    )
    return report
