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
    lnk_args: str = ""  # argumentos extraídos do .lnk (se veio de um atalho)

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
    lnk_args = ""
    lnk_cwd: Path | None = None

    # Resolver .lnk (atalhos do Windows)
    if path.suffix.lower() == ".lnk":
        try:
            resolved = _resolve_lnk(path)
        except Exception:
            resolved = None
        if resolved and isinstance(resolved, tuple) and len(resolved) == 3:
            target, args, working_dir = resolved
            # Garantir strings puras Python
            args = str(args) if args else ""
            working_dir = str(working_dir) if working_dir else ""
            path = target if isinstance(target, Path) else Path(str(target))
            lnk_args = args
            if working_dir:
                lnk_cwd = Path(working_dir)

            # Se o target for wscript.exe/cscript.exe e há args apontando para
            # um .vbs, o script real é o que nos interessa — usa o script do arg
            target_name = path.name.lower()
            if target_name in ("wscript.exe", "cscript.exe") and args:
                # Extrair o primeiro caminho dos args (entre aspas ou não)
                script_path = _extract_first_path(args)
                if script_path and script_path.suffix.lower() == ".vbs":
                    path = script_path
                    # Demais args (após o script) mantém como lnk_args
                    lnk_args = _strip_first_path(args)

    ext = path.suffix.lower()
    cwd = lnk_cwd if lnk_cwd and lnk_cwd.exists() else path.parent

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

    return ExecutableInfo(
        kind=kind, path=path, cwd=cwd, venv_python=venv_python, lnk_args=lnk_args
    )


def build_command(path_str: str, arguments: str = "") -> tuple[str, str]:
    """Monta (cmd_string, cwd_string) para o config.yaml.

    cmd_string é o que vai em `cmd:` no YAML.
    cwd_string é o que vai em `cwd:` no YAML.
    """
    info = detect(path_str)
    # Combinar args do .lnk com args passados explicitamente
    combined = " ".join(p for p in (info.lnk_args.strip(), arguments.strip()) if p)
    args = combined

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


def _resolve_lnk(lnk_path: Path) -> tuple[Path, str, str] | None:
    """Resolve um atalho .lnk. Retorna (target, arguments, working_directory).

    Usa COM via pywin32 quando disponível. Caso contrário, cai para um
    parser binário simples (apenas o target).
    """
    # 1) pywin32 (COM) — converter explicitamente tudo para str Python
    try:
        import win32com.client  # type: ignore
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        target = str(shortcut.Targetpath) if shortcut.Targetpath else ""
        args = str(shortcut.Arguments) if shortcut.Arguments else ""
        wd = str(shortcut.WorkingDirectory) if shortcut.WorkingDirectory else ""
        # Liberar o objeto COM explicitamente
        shortcut = None
        shell = None
        if target:
            return Path(target), args, wd
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Falha ao resolver .lnk via COM: {e}")

    # 2) Parser binário básico (estilo pylnk) — só extrai target se puder
    try:
        raw = lnk_path.read_bytes()
        # Busca heurística: caminhos de arquivo ASCII terminados por \0
        # dentro do bloco LinkTargetIDList
        candidates = []
        i = 0
        while i < len(raw) - 3:
            # Padrão: letra + ':' + '\\'
            if 65 <= raw[i] <= 122 and raw[i + 1] == 0x3A and raw[i + 2] == 0x5C:
                end = raw.find(b"\x00", i)
                if end > i:
                    try:
                        s = raw[i:end].decode("latin-1", errors="replace")
                        if "." in s and " " not in s[:3]:
                            candidates.append(s)
                    except Exception:
                        pass
                    i = end + 1
                    continue
            i += 1
        if candidates:
            return Path(candidates[0]), "", ""
    except Exception:
        pass
    return None


def _tokenize_args(args: str) -> list[str]:
    """Tokeniza argumentos respeitando aspas."""
    import shlex as _shlex
    try:
        return _shlex.split(args, posix=False)
    except ValueError:
        return args.split()


def _extract_first_path(args: str) -> Path | None:
    """Extrai o primeiro caminho real (ignorando flags como //nologo, /s)."""
    for token in _tokenize_args(args):
        t = token.strip('"')
        if not t:
            continue
        # Pular flags: começam com / ou //
        if t.startswith("/") and not (len(t) >= 2 and t[1] == ":"):
            # Mas permitir paths absolutos Unix-style /c/... (não comum no win)
            if not t.startswith("//") or (len(t) > 2 and t[2] != "/"):
                # É uma flag como /s, //nologo, //B
                continue
        return Path(t)
    return None


def _strip_first_path(args: str) -> str:
    """Remove o primeiro caminho real da string de argumentos (mantém as flags)."""
    tokens = _tokenize_args(args)
    result = []
    found_path = False
    for token in tokens:
        t = token.strip('"')
        if not found_path:
            # Flag — mantém
            if t.startswith("/") and not (len(t) >= 2 and t[1] == ":"):
                result.append(token)
                continue
            # É o primeiro caminho — pula
            found_path = True
            continue
        # Após o path, mantém tudo
        result.append(token)
    return " ".join(result).strip()
