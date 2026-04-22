"""Detecta o tipo de executável pela extensão e monta o comando correto.

Suporta:
  - .py  → usa .venv/Scripts/python da pasta pai se existir, senão python global
  - .exe → invoca direto
  - .bat / .cmd → cmd /c "caminho"
  - .ps1 → powershell -ExecutionPolicy Bypass -File "caminho"
  - .vbs → cscript //nologo //B "caminho"  (modo silencioso, sem GUI)
  - .lnk → resolve o target do atalho do Windows
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecutableInfo:
    kind: str  # python, exe, batch, powershell, vbs, unknown
    path: Path
    cwd: Path  # pasta pai do executável
    venv_python: Path | None = None  # .venv\Scripts\python.exe se existir

    @property
    def display_kind(self) -> str:
        return {
            "python": "Python",
            "exe": "Executável",
            "batch": "Batch",
            "powershell": "PowerShell",
            "vbs": "VBScript",
            "unknown": "Desconhecido",
        }.get(self.kind, self.kind)

    @property
    def icon(self) -> str:
        return {
            "python": "🐍",
            "exe": "⚙️",
            "batch": "📜",
            "powershell": "💠",
            "vbs": "🧩",
            "unknown": "📄",
        }.get(self.kind, "📄")


def detect(path_str: str) -> ExecutableInfo:
    """Detecta o tipo de arquivo executável e retorna informações."""
    path = Path(path_str)

    # Resolver .lnk (atalhos do Windows)
    if path.suffix.lower() == ".lnk":
        resolved = _resolve_lnk(path)
        if resolved:
            path = resolved

    ext = path.suffix.lower()
    cwd = path.parent if path.exists() else path.parent

    kind = {
        ".py": "python",
        ".exe": "exe",
        ".bat": "batch",
        ".cmd": "batch",
        ".ps1": "powershell",
        ".vbs": "vbs",
    }.get(ext, "unknown")

    venv_python = None
    if kind == "python":
        # Procura .venv\Scripts\python.exe em pastas pais (até 3 níveis)
        check_cwd: Path | None = cwd
        for _ in range(3):
            if check_cwd is None:
                break
            candidate = check_cwd / ".venv" / "Scripts" / "python.exe"
            if candidate.exists():
                venv_python = candidate
                cwd = check_cwd  # ajusta cwd pra raiz do projeto
                break
            # Stop se já chegamos na raiz do drive
            if check_cwd.parent == check_cwd:
                break
            check_cwd = check_cwd.parent

    return ExecutableInfo(kind=kind, path=path, cwd=cwd, venv_python=venv_python)


def build_command(path_str: str, arguments: str = "") -> tuple[str, str]:
    """Monta (cmd_string, cwd_string) para o config.yaml.

    cmd_string é o que vai em `cmd:` no YAML.
    cwd_string é o que vai em `cwd:` no YAML.
    """
    info = detect(path_str)
    args = arguments.strip()

    if info.kind == "python":
        # Se achou venv, usa caminho relativo ao cwd
        if info.venv_python:
            python_rel = ".venv/Scripts/python"
            try:
                py_path_rel = info.path.relative_to(info.cwd).as_posix()
            except ValueError:
                py_path_rel = str(info.path).replace("\\", "/")
            cmd = f'{python_rel} {py_path_rel}'
        else:
            cmd = f'python "{info.path}"'
        if args:
            cmd = f"{cmd} {args}"

    elif info.kind == "exe":
        cmd = f'"{info.path}"' if " " in str(info.path) else str(info.path)
        if args:
            cmd = f"{cmd} {args}"

    elif info.kind == "batch":
        cmd = f'cmd /c "{info.path}"'
        if args:
            cmd = f"{cmd} {args}"

    elif info.kind == "powershell":
        cmd = f'powershell -ExecutionPolicy Bypass -File "{info.path}"'
        if args:
            cmd = f"{cmd} {args}"

    elif info.kind == "vbs":
        # //nologo suprime banner; //B = batch mode (sem prompts/erros em GUI)
        cmd = f'cscript //nologo //B "{info.path}"'
        if args:
            cmd = f"{cmd} {args}"

    else:
        cmd = f'"{info.path}" {args}'.strip()

    return cmd, str(info.cwd).replace("\\", "/")


def parse_command(cmd: str, cwd: str) -> tuple[str, str, str]:
    """Inverso — extrai (path_exe, argumentos, tipo) de um comando já salvo.

    Usado ao editar uma automação no formulário.
    """
    parts = shlex.split(cmd, posix=False)
    if not parts:
        return "", "", "unknown"

    first = parts[0].strip('"')
    lower = first.lower()

    # Python via venv
    if ".venv" in lower and "python" in lower:
        if len(parts) > 1:
            script_rel = parts[1].strip('"')
            script_path = (Path(cwd) / script_rel).as_posix()
            rest = " ".join(parts[2:])
            return script_path, rest, "python"
        return "", "", "python"

    # cmd /c
    if lower == "cmd" and len(parts) > 2 and parts[1].lower() == "/c":
        return parts[2].strip('"'), " ".join(parts[3:]), "batch"

    # cscript / wscript (VBScript)
    if "cscript" in lower or "wscript" in lower:
        # achar primeiro argumento que não é flag
        for p in parts[1:]:
            if not p.startswith("//") and not p.startswith("/"):
                return p.strip('"'), "", "vbs"

    # powershell
    if "powershell" in lower:
        # achar -File "path"
        for i, p in enumerate(parts):
            if p.lower() == "-file" and i + 1 < len(parts):
                return parts[i + 1].strip('"'), " ".join(parts[i + 2:]), "powershell"

    # python global
    if "python" in lower and len(parts) > 1:
        return parts[1].strip('"'), " ".join(parts[2:]), "python"

    # exe direto
    if first.endswith(".exe"):
        return first, " ".join(parts[1:]), "exe"

    return first, " ".join(parts[1:]), "unknown"


def _resolve_lnk(lnk_path: Path) -> Path | None:
    """Resolve o target de um atalho .lnk do Windows."""
    try:
        import struct
        with open(lnk_path, "rb") as f:
            content = f.read()
        # Parse simplificado: procura pela seção LinkInfo
        # Fallback: usa COM via pywin32 se disponível
        try:
            import win32com.client  # type: ignore
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(lnk_path))
            target = shortcut.Targetpath
            if target:
                return Path(target)
        except ImportError:
            pass
        # Parse binário básico — extrai primeiro caminho encontrado
        _ = struct
        return None
    except Exception:
        return None
