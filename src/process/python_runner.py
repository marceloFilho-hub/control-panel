"""Runner nativo para apps Python — sem camadas de VBS/bat/lnk.

O usuário informa o caminho do script principal (.py) e o Control Panel:
  1. Descobre o Python correto: busca `.venv/Scripts/python.exe` ou
     `.venv/Scripts/pythonw.exe` em pastas ancestrais do script
  2. Descobre o `.env` do projeto: busca nas mesmas pastas ancestrais
  3. Monta o comando com quoting correto e carrega as env vars
  4. Lança direto como subprocess (sem VBS intermediário)

Para apps GUI (Tkinter, PyQt), usa `pythonw.exe` (sem console) e o
caller deve passar `gui=True` para `process_manager` aplicar
CREATE_BREAKAWAY_FROM_JOB e o processo herdar o desktop interativo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PythonProject:
    """Informações descobertas sobre um projeto Python."""
    script_path: Path          # caminho absoluto do .py main
    project_root: Path         # pasta onde está o .venv (ou pai do script)
    python_exe: Path | None    # .venv\Scripts\python.exe (se existir)
    pythonw_exe: Path | None   # .venv\Scripts\pythonw.exe (se existir)
    env_file: Path | None      # .env do projeto (se existir)

    @property
    def has_venv(self) -> bool:
        return self.python_exe is not None and self.python_exe.exists()

    @property
    def display_path(self) -> str:
        return str(self.script_path).replace("\\", "/")


def detect_project(script_path_str: str) -> PythonProject:
    """Analisa o caminho de um .py e descobre venv/.env do projeto.

    Busca em até 5 pastas ancestrais por:
      - `.venv/Scripts/python.exe` (Windows) — raiz do projeto
      - `.env` — na mesma pasta do venv

    Se não encontrar venv, cai no Python global.
    """
    script_path = Path(script_path_str).resolve()

    python_exe: Path | None = None
    pythonw_exe: Path | None = None
    env_file: Path | None = None
    project_root: Path = script_path.parent

    # Busca recursiva por pastas pai (até 5 níveis)
    candidate = script_path.parent
    for _ in range(5):
        # Procurar .venv
        venv_py = candidate / ".venv" / "Scripts" / "python.exe"
        venv_pyw = candidate / ".venv" / "Scripts" / "pythonw.exe"
        if venv_py.exists():
            python_exe = venv_py
            if venv_pyw.exists():
                pythonw_exe = venv_pyw
            project_root = candidate

            # Procurar .env no mesmo nível do venv
            env_candidate = candidate / ".env"
            if env_candidate.exists():
                env_file = env_candidate
            break

        if candidate.parent == candidate:  # raiz do drive
            break
        candidate = candidate.parent

    # Se não achou .env junto com venv, procurar do script pra cima também
    if env_file is None:
        candidate = script_path.parent
        for _ in range(5):
            e = candidate / ".env"
            if e.exists():
                env_file = e
                break
            if candidate.parent == candidate:
                break
            candidate = candidate.parent

    return PythonProject(
        script_path=script_path,
        project_root=project_root,
        python_exe=python_exe,
        pythonw_exe=pythonw_exe,
        env_file=env_file,
    )


def build_command(
    project: PythonProject,
    args: str = "",
    gui: bool = False,
) -> tuple[str, str]:
    """Monta o comando final (cmd_str, cwd_str) para rodar o script.

    - gui=True: usa pythonw.exe (sem console) quando disponível
    - Quando não há venv detectado, usa 'python' global

    O cwd é a pasta do projeto (onde fica o .venv ou o script).
    """
    # Escolher executável
    if gui and project.pythonw_exe and project.pythonw_exe.exists():
        py = project.pythonw_exe
    elif project.python_exe and project.python_exe.exists():
        py = project.python_exe
    else:
        # Fallback: python/pythonw global
        py = Path("pythonw" if gui else "python")

    # Aspas só quando o path tem espaço
    py_str = str(py)
    script_str = str(project.script_path)

    def q(s: str) -> str:
        return f'"{s}"' if " " in s else s

    cmd = f"{q(py_str)} {q(script_str)}"
    if args.strip():
        cmd = f"{cmd} {args.strip()}"

    cwd = str(project.project_root).replace("\\", "/")
    return cmd, cwd


def load_env_file(env_path: Path | None) -> dict[str, str]:
    """Lê um .env simples e retorna dict de variáveis.

    Suporta: KEY=VALUE, comentários com #, aspas simples/duplas opcionais.
    Ignora silenciosamente linhas inválidas.
    """
    if env_path is None or not env_path.exists():
        return {}

    result: dict[str, str] = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except Exception:
        try:
            content = env_path.read_text(encoding="latin-1")
        except Exception:
            return {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Remover prefixo "export " se presente
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # Remover aspas externas
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"')
            or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        result[key] = val

    return result
