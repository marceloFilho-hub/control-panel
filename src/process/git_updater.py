"""Auto-update de repos git antes de cada rodada de app.

Filosofia: zero configuração por app. O orquestrador descobre o repo a
partir do `cwd` cadastrado e faz `git fetch + git reset --hard <remote>/<branch>`
entre rodagens, com slot e gate de memória já adquiridos. Se o cwd não for
parte de um repo git, ou o repo não tiver remote configurado, é no-op
silencioso.

Uso típico (a partir do orchestrator):

    from .process.git_updater import discover_repo, update_repo

    info = discover_repo(Path(app.cwd))
    if info and info.has_remote:
        result = await update_repo(info, timeout=60.0)
        if not result.success:
            ...  # abortar este ciclo
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoInfo:
    """Identifica unicamente um repo git para fins de auto-update."""

    root: Path  # path absoluto resolvido até o diretório que contém .git
    branch: str  # branch atualmente checked out
    remote: str  # nome do remote (ex: "origin")
    has_remote: bool  # False se o repo não tem remote configurado


@dataclass
class UpdateResult:
    success: bool
    duration_ms: int
    error: str = ""  # vazio quando success=True
    output: str = ""  # stdout+stderr do fetch/reset (truncado)


def _find_git_root(start: Path) -> Path | None:
    """Sobe os pais a partir de `start` até achar um diretório com `.git`.

    Retorna None se chegar à raiz do sistema sem encontrar.
    """
    try:
        current = start.resolve()
    except (OSError, RuntimeError):
        return None

    if current.is_file():
        current = current.parent

    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


async def _run_git(
    args: list[str], cwd: Path, timeout: float
) -> tuple[int, str]:
    """Executa `git <args>` capturando stdout+stderr unificados."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return -1, "git nao encontrado no PATH"
    except Exception as e:
        return -1, f"falha ao iniciar git: {e}"

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "timeout"

    text = (out or b"").decode("utf-8", errors="replace")
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, text


async def discover_repo(cwd: Path | str, timeout: float = 5.0) -> RepoInfo | None:
    """Descobre repo+branch+remote a partir do cwd de um app.

    Retorna None se `cwd` não está dentro de nenhum repo git ou se a branch
    atual não puder ser determinada (ex: detached HEAD). Repos sem remote
    configurado retornam `RepoInfo` com `has_remote=False` para que o caller
    possa decidir o comportamento (atualmente: skip).
    """
    cwd_path = Path(cwd) if not isinstance(cwd, Path) else cwd
    root = _find_git_root(cwd_path)
    if root is None:
        return None

    rc, branch_out = await _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], root, timeout
    )
    if rc != 0:
        return None
    branch = branch_out.strip()
    if not branch or branch == "HEAD":
        # detached HEAD — sem branch nominal, não dá pra fazer reset --hard
        # contra um remote/branch
        return None

    rc, remote_out = await _run_git(
        ["config", "--get", f"branch.{branch}.remote"], root, timeout
    )
    remote = remote_out.strip() if rc == 0 else ""
    has_remote = bool(remote)
    if not has_remote:
        # fallback comum: origin existe?
        rc_origin, _ = await _run_git(
            ["config", "--get", "remote.origin.url"], root, timeout
        )
        if rc_origin == 0:
            remote = "origin"
            has_remote = True

    return RepoInfo(
        root=root.resolve(), branch=branch, remote=remote, has_remote=has_remote
    )


async def update_repo(info: RepoInfo, timeout: float = 60.0) -> UpdateResult:
    """Executa fetch + reset --hard <remote>/<branch> no repo informado.

    Estratégia única, sem opção. O servidor de execução não deve ter
    alterações locais que não estejam refletidas no remoto — qualquer
    coisa local é descartada.

    Time orçamentário (`timeout`) cobre fetch + reset somados.
    """
    import time as _time

    start = _time.monotonic()

    if not info.has_remote:
        return UpdateResult(success=True, duration_ms=0, output="(sem remote — skip)")

    half = max(1.0, timeout / 2.0)

    rc, fetch_out = await _run_git(
        ["fetch", "--quiet", info.remote, info.branch],
        info.root,
        timeout=half,
    )
    if rc != 0:
        elapsed = int((_time.monotonic() - start) * 1000)
        return UpdateResult(
            success=False,
            duration_ms=elapsed,
            error=f"git fetch falhou (rc={rc})",
            output=fetch_out[-500:],
        )

    remaining = max(1.0, timeout - (_time.monotonic() - start))
    target = f"{info.remote}/{info.branch}"
    rc, reset_out = await _run_git(
        ["reset", "--hard", target],
        info.root,
        timeout=remaining,
    )
    elapsed = int((_time.monotonic() - start) * 1000)
    if rc != 0:
        return UpdateResult(
            success=False,
            duration_ms=elapsed,
            error=f"git reset --hard {target} falhou (rc={rc})",
            output=reset_out[-500:],
        )

    return UpdateResult(
        success=True,
        duration_ms=elapsed,
        output=(fetch_out + reset_out)[-500:],
    )
