"""Configuração comum dos testes do control_panel."""

from __future__ import annotations

import sys
from pathlib import Path

# Garante que `src.*` seja importável quando rodar `pytest` da raiz do projeto.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
