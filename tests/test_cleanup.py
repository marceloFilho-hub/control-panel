"""Testes do módulo `src.process.cleanup`.

Foco: lógica pura e contratos defensivos. Não matamos processos reais
nem mexemos no %TEMP% do usuário — usamos tmp_path e diretórios stub.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.process.cleanup import (
    DEFAULT_ORPHAN_NAMES,
    CleanupReport,
    kill_orphans_by_name,
    purge_pycache,
    purge_temp_dirs,
)


# ──────────────────────────────────────────────────────────────────
# kill_orphans_by_name
# ──────────────────────────────────────────────────────────────────


class TestKillOrphans:
    def test_lista_vazia_retorna_zero(self) -> None:
        assert kill_orphans_by_name([]) == 0

    def test_tupla_vazia_retorna_zero(self) -> None:
        assert kill_orphans_by_name(()) == 0

    def test_lista_so_com_strings_vazias_retorna_zero(self) -> None:
        # Filtro interno (`if n`) deve descartar entradas vazias e não percorrer
        # toda a tabela de processos atrás de nada.
        assert kill_orphans_by_name(["", "", ""]) == 0

    def test_nome_inexistente_nao_mata_nada(self) -> None:
        # Nome implausível garante que jamais haverá match real na máquina.
        nome = "este_processo_nao_deve_existir_jamais_xyz_123.exe"
        assert kill_orphans_by_name([nome]) == 0

    def test_exclude_pids_protege_pid_proprio(self) -> None:
        # Ainda que o nome do processo do teste case com um alvo improvável,
        # passar o PID na exclusão tem que blindar.
        assert (
            kill_orphans_by_name(
                ["processo_que_nao_existe.exe"],
                exclude_pids={os.getpid()},
            )
            == 0
        )

    def test_default_orphan_names_contem_drivers_conhecidos(self) -> None:
        # Sanity check: lista padrão precisa ao menos pegar os drivers que
        # motivaram a feature (chromedriver, geckodriver, msedgedriver).
        names = {n.lower() for n in DEFAULT_ORPHAN_NAMES}
        assert "chromedriver.exe" in names
        assert "geckodriver.exe" in names
        assert "msedgedriver.exe" in names


# ──────────────────────────────────────────────────────────────────
# purge_pycache
# ──────────────────────────────────────────────────────────────────


def _make_pycache(parent: Path, content: bytes = b"x" * 1024) -> Path:
    """Cria um __pycache__ realista (com 1 .pyc dentro)."""
    cache = parent / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "module.cpython-311.pyc").write_bytes(content)
    return cache


class TestPurgePycache:
    def test_remove_pycache_simples(self, tmp_path: Path) -> None:
        cache = _make_pycache(tmp_path)
        assert cache.exists()
        freed_mb = purge_pycache(tmp_path)
        assert not cache.exists()
        # Liberou algo, ainda que menos de 1 MB (não logamos, mas retornamos).
        assert freed_mb >= 0.0

    def test_preserva_pycache_dentro_de_venv(self, tmp_path: Path) -> None:
        # Estrutura tmp/.venv/Lib/site-packages/foo/__pycache__/
        venv_cache = _make_pycache(tmp_path / ".venv" / "Lib" / "site-packages" / "foo")
        # E um __pycache__ "legítimo" fora do .venv para garantir que ele SIM é removido.
        app_cache = _make_pycache(tmp_path / "src" / "modulo")

        purge_pycache(tmp_path)

        assert venv_cache.exists(), ".venv/__pycache__ NUNCA pode ser removido"
        assert not app_cache.exists(), "__pycache__ fora do .venv deve ser removido"

    def test_preserva_pycache_dentro_de_dot_git(self, tmp_path: Path) -> None:
        git_cache = _make_pycache(tmp_path / ".git" / "hooks")
        purge_pycache(tmp_path)
        assert git_cache.exists(), ".git/__pycache__ nunca pode ser removido"

    def test_preserva_pycache_dentro_de_node_modules(self, tmp_path: Path) -> None:
        node_cache = _make_pycache(tmp_path / "node_modules" / "x")
        purge_pycache(tmp_path)
        assert node_cache.exists()

    def test_root_inexistente_retorna_zero(self, tmp_path: Path) -> None:
        assert purge_pycache(tmp_path / "nao_existe") == 0.0

    def test_root_arquivo_retorna_zero(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "arquivo.txt"
        arquivo.write_text("oi")
        assert purge_pycache(arquivo) == 0.0

    def test_pycache_aninhado_removido(self, tmp_path: Path) -> None:
        # Múltiplos __pycache__ em subdirs distintos
        c1 = _make_pycache(tmp_path / "a")
        c2 = _make_pycache(tmp_path / "a" / "b")
        c3 = _make_pycache(tmp_path / "c" / "d" / "e")
        purge_pycache(tmp_path)
        assert not c1.exists()
        assert not c2.exists()
        assert not c3.exists()

    def test_max_depth_respeitado(self, tmp_path: Path) -> None:
        # Cache fundo demais: max_depth=1 não deve descer até ele.
        fundo = _make_pycache(tmp_path / "a" / "b" / "c" / "d")
        raso = _make_pycache(tmp_path / "raso")
        purge_pycache(tmp_path, max_depth=1)
        assert fundo.exists(), "max_depth=1 não deve alcançar profundidade 4"
        assert not raso.exists(), "raso (profundidade 1) deve ser removido"


# ──────────────────────────────────────────────────────────────────
# purge_temp_dirs
# ──────────────────────────────────────────────────────────────────


class TestPurgeTempDirs:
    def test_arquivo_recente_nao_eh_removido(self, tmp_path: Path) -> None:
        # Arquivo recém-criado: mtime ~ agora. min_age=300 ⇒ tem que sobrar.
        recente = tmp_path / "lock_em_uso.tmp"
        recente.write_bytes(b"app rodando agora")
        freed = purge_temp_dirs(min_age_seconds=300, dirs=[tmp_path])
        assert recente.exists(), "arquivo recente NUNCA deve ser removido"
        assert freed == 0.0

    def test_arquivo_antigo_eh_removido(self, tmp_path: Path) -> None:
        antigo = tmp_path / "lixo_velho.tmp"
        antigo.write_bytes(b"x" * 2048)
        # Joga mtime para 1h atrás.
        past = time.time() - 3600
        os.utime(antigo, (past, past))

        freed = purge_temp_dirs(min_age_seconds=300, dirs=[tmp_path])
        assert not antigo.exists()
        assert freed > 0.0

    def test_min_age_zero_remove_tudo(self, tmp_path: Path) -> None:
        f = tmp_path / "qualquer.tmp"
        f.write_bytes(b"x")
        # mtime no passado p/ não cair em corrida com cutoff
        past = time.time() - 10
        os.utime(f, (past, past))
        purge_temp_dirs(min_age_seconds=0, dirs=[tmp_path])
        assert not f.exists()

    def test_dirs_vazio_retorna_zero(self) -> None:
        assert purge_temp_dirs(dirs=[]) == 0.0

    def test_dir_inexistente_nao_quebra(self, tmp_path: Path) -> None:
        # Função silencia OSError — não pode levantar.
        freed = purge_temp_dirs(dirs=[tmp_path / "nao_existe"])
        assert freed == 0.0

    def test_subdir_antigo_eh_removido_recursivamente(self, tmp_path: Path) -> None:
        sub = tmp_path / "playwright_xyz"
        sub.mkdir()
        (sub / "f1").write_bytes(b"x" * 512)
        (sub / "f2").write_bytes(b"y" * 512)
        past = time.time() - 3600
        for p in [sub, sub / "f1", sub / "f2"]:
            os.utime(p, (past, past))
        freed = purge_temp_dirs(min_age_seconds=300, dirs=[tmp_path])
        assert not sub.exists()
        assert freed > 0.0


# ──────────────────────────────────────────────────────────────────
# CleanupReport
# ──────────────────────────────────────────────────────────────────


class TestCleanupReport:
    def test_summary_vazio(self) -> None:
        r = CleanupReport()
        assert r.summary() == "nada a limpar"
        assert r.total_freed_mb == 0.0

    def test_summary_so_orphans(self) -> None:
        r = CleanupReport(orphans_killed=3)
        s = r.summary()
        assert "3 órfão(s)" in s

    def test_summary_completo(self) -> None:
        r = CleanupReport(
            orphans_killed=2,
            pycache_freed_mb=12.5,
            temp_freed_mb=300.0,
            recycle_bin_ok=True,
            ram_freed_mb=512.0,
        )
        s = r.summary()
        assert "2 órfão(s)" in s
        assert "12.5 MB __pycache__" in s
        assert "300.0 MB temp" in s
        assert "lixeira" in s
        assert "RAM" in s

    def test_total_freed_mb_soma_pycache_e_temp(self) -> None:
        r = CleanupReport(pycache_freed_mb=10.0, temp_freed_mb=25.5)
        assert r.total_freed_mb == pytest.approx(35.5)

    def test_pycache_zero_nao_aparece_no_summary(self) -> None:
        # Se um campo é zero, summary não polui com "0.0 MB ...".
        r = CleanupReport(orphans_killed=1, pycache_freed_mb=0.0)
        assert "MB __pycache__" not in r.summary()
        assert "1 órfão(s)" in r.summary()

    def test_details_default_eh_lista_independente(self) -> None:
        # Bug clássico de dataclass: default mutável compartilhado.
        a = CleanupReport()
        b = CleanupReport()
        a.details.append("a1")
        assert b.details == [], "default_factory deve dar listas independentes"
