"""Windows Job Objects — matar árvore completa de processos atomicamente.

Um Windows Job Object agrupa processos filhos/netos/bisnetos. Quando o
job é fechado (ou TerminateJobObject é chamado) com a flag
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, TODOS os processos do job morrem
de uma vez — mesmo os que foram desanexados do processo pai ou
promovidos a órfãos.

Isso é a forma CORRETA de garantir cleanup de processos no Windows.
É exatamente o mecanismo usado pelo Docker/Podman no Windows, pelo
VS Code para matar terminals, e por CI runners.

Em sistemas não-Windows ou sem pywin32, vira no-op (fallback: o caller
deve usar kill_process_tree via psutil).
"""

from __future__ import annotations

import platform
import time

IS_WINDOWS = platform.system() == "Windows"

HAS_JOB = False
if IS_WINDOWS:
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32job  # type: ignore

        HAS_JOB = True
    except ImportError:
        HAS_JOB = False


class JobObject:
    """Wrapper cross-platform para Windows Job Object.

    Uso típico:
        job = JobObject(name="meu_app")
        proc = subprocess.Popen(...)
        job.assign(proc.pid)        # anexa o processo ao job
        ...
        job.terminate()             # mata TODA a árvore de uma vez
        job.close()                 # libera o handle
    """

    def __init__(self, name: str = "app"):
        self.name = name
        self.handle = None
        self._assigned_pids: list[int] = []
        if not HAS_JOB:
            return
        try:
            # Nome único por instância para evitar colisão
            job_name = f"ControlPanel_{name}_{int(time.time() * 1000)}"
            self.handle = win32job.CreateJobObject(None, job_name)

            # Configurar kill-on-close: ao fechar o handle, todos os processos
            # do job são automaticamente terminados.
            info = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.handle,
                win32job.JobObjectExtendedLimitInformation,
                info,
            )
        except Exception:
            self.handle = None

    @property
    def available(self) -> bool:
        """True se o Job Object foi criado com sucesso (Windows + pywin32)."""
        return self.handle is not None

    def assign(self, pid: int) -> bool:
        """Atribui um processo (e futuros descendentes) ao job.

        Deve ser chamado IMEDIATAMENTE após criar o subprocess para garantir
        que os filhos que ele spawnar também entrem no job automaticamente.
        """
        if not self.available or not HAS_JOB:
            return False
        try:
            perm = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
            proc_handle = win32api.OpenProcess(perm, False, pid)
            try:
                win32job.AssignProcessToJobObject(self.handle, proc_handle)
                self._assigned_pids.append(pid)
                return True
            finally:
                win32api.CloseHandle(proc_handle)
        except Exception:
            return False

    def count_alive(self) -> int:
        """Conta quantos processos do job ainda estão vivos."""
        if not self.available:
            return 0
        try:
            import psutil
            count = 0
            for pid in self._assigned_pids:
                try:
                    p = psutil.Process(pid)
                    if p.is_running():
                        count += 1
                    # Também conta descendentes ainda vivos
                    count += sum(
                        1 for c in p.children(recursive=True) if c.is_running()
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return count
        except Exception:
            return 0

    def terminate(self, exit_code: int = 1) -> int:
        """Termina TODOS os processos do job. Retorna quantos estavam vivos."""
        if not self.available or not HAS_JOB:
            return 0
        alive = self.count_alive()
        try:
            win32job.TerminateJobObject(self.handle, exit_code)
        except Exception:
            pass
        return alive

    def close(self) -> None:
        """Libera o handle do job.

        Com JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, o fechamento do handle
        implicitamente mata processos ainda vivos do job — é o failsafe
        definitivo.
        """
        if self.handle is None:
            return
        if HAS_JOB:
            try:
                win32api.CloseHandle(self.handle)
            except Exception:
                pass
        self.handle = None

    def __del__(self) -> None:
        # Garantir cleanup mesmo se o caller esquecer
        self.close()
