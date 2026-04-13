"""Dashboard Streamlit — painel de controle do Hidra Control Plane."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from state import AppState, ControlPlaneState, load_state

COMMANDS_DIR = Path(__file__).parent.parent / "commands"
COMMANDS_DIR.mkdir(exist_ok=True)

# ── Cores do dark mode padrão BHub ───────────────────────────
BG_BASE = "#0F172A"
BG_CARD = "#1E293B"
BORDER = "#334155"
TEXT_MAIN = "#F1F5F9"
TEXT_MUTED = "#94A3B8"
SUCCESS = "#10B981"
ERROR = "#EF4444"
WARNING = "#F59E0B"
INFO = "#3B82F6"

STATUS_COLORS = {
    "running": SUCCESS,
    "done": SUCCESS,
    "off": TEXT_MUTED,
    "queued": INFO,
    "failed": ERROR,
    "timeout": WARNING,
}

STATUS_ICONS = {
    "running": "\u25cf",
    "done": "\u2713",
    "off": "\u25cb",
    "queued": "\u23f3",
    "failed": "\u2717",
    "timeout": "\u23f0",
}


def trigger_app(app_name: str) -> None:
    """Cria um arquivo .trigger para o orquestrador executar o app."""
    (COMMANDS_DIR / f"run_{app_name}.trigger").touch()


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    hours = minutes / 60
    return f"{hours:.1f}h"


def format_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def render_kpi_row(state: ControlPlaneState) -> None:
    """Renderiza a linha de KPIs no topo."""
    apps = state.apps.values()
    total = len(list(apps))
    running = sum(1 for a in state.apps.values() if a.status == "running")
    failed = sum(1 for a in state.apps.values() if a.status == "failed")
    queued = sum(1 for a in state.apps.values() if a.status == "queued")

    cols = st.columns(6)
    with cols[0]:
        st.metric("Apps", total)
    with cols[1]:
        st.metric("Rodando", running)
    with cols[2]:
        st.metric("Na fila", queued)
    with cols[3]:
        st.metric("Falhas", failed)
    with cols[4]:
        st.metric("RAM VM", f"{state.total_ram_mb / 1024:.1f} GB")
    with cols[5]:
        st.metric("CPU VM", f"{state.total_cpu_pct:.0f}%")


def render_slots(state: ControlPlaneState) -> None:
    """Renderiza indicadores de slots."""
    c1, c2 = st.columns(2)
    with c1:
        used = state.heavy_slots_used
        total = state.heavy_slots_max
        bar_pct = (used / total) if total else 0
        st.markdown(f"**Slot Heavy:** {used}/{total}")
        st.progress(bar_pct)
    with c2:
        used = state.light_slots_used
        total = state.light_slots_max
        bar_pct = (used / total) if total else 0
        st.markdown(f"**Slot Light:** {used}/{total}")
        st.progress(bar_pct)


def render_app_table(state: ControlPlaneState) -> None:
    """Renderiza a tabela de apps com status e ações."""
    # Separar por tipo
    always_apps = {k: v for k, v in state.apps.items() if v.slot == "always"}
    heavy_apps = {k: v for k, v in state.apps.items() if v.slot == "heavy"}
    light_apps = {k: v for k, v in state.apps.items() if v.slot == "light"}

    for section_name, apps in [
        ("Servicos Always-On", always_apps),
        ("Jobs Heavy (1 por vez)", heavy_apps),
        ("Jobs Light (ate 3 paralelos)", light_apps),
    ]:
        if not apps:
            continue
        st.markdown(f"### {section_name}")
        for name, app in sorted(apps.items(), key=lambda x: x[1].status != "running"):
            _render_app_row(name, app)


def _render_app_row(name: str, app: AppState) -> None:
    """Renderiza uma linha de app."""
    icon = STATUS_ICONS.get(app.status, "?")
    color = STATUS_COLORS.get(app.status, TEXT_MUTED)

    cols = st.columns([3, 1.5, 1, 1, 1.5, 1.5, 1])

    with cols[0]:
        st.markdown(
            f"<span style='color:{color};font-size:1.1em'>{icon}</span> **{name}**",
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.caption(app.status.upper())
    with cols[2]:
        if app.ram_mb > 0:
            st.caption(f"{app.ram_mb:.0f} MB")
        else:
            st.caption("—")
    with cols[3]:
        if app.cpu_pct > 0:
            st.caption(f"{app.cpu_pct:.0f}%")
        else:
            st.caption("—")
    with cols[4]:
        st.caption(format_time(app.started_at) if app.status == "running" else format_time(app.finished_at))
    with cols[5]:
        st.caption(app.next_run or "—")
    with cols[6]:
        if app.status not in ("running", "queued"):
            if st.button("Run", key=f"run_{name}", type="primary"):
                trigger_app(name)
                st.rerun()


def render_history(state: ControlPlaneState) -> None:
    """Renderiza histórico de execuções recentes."""
    completed = [
        a for a in state.apps.values()
        if a.finished_at and a.status in ("done", "failed", "timeout")
    ]
    completed.sort(key=lambda a: a.finished_at or 0, reverse=True)

    if not completed:
        st.info("Nenhuma execucao concluida ainda.")
        return

    for app in completed[:10]:
        icon = STATUS_ICONS.get(app.status, "?")
        color = STATUS_COLORS.get(app.status, TEXT_MUTED)
        duration = format_duration(app.last_duration_s)
        finished = format_time(app.finished_at)

        st.markdown(
            f"<span style='color:{color}'>{icon}</span> "
            f"**{app.name}** — {app.status} — {duration} — {finished}"
            + (f" — `{app.last_error[:80]}`" if app.last_error else ""),
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(
        page_title="Hidra Control Plane",
        page_icon="\U0001f40d",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # CSS dark mode
    st.markdown(f"""
    <style>
        .stApp {{ background-color: {BG_BASE}; color: {TEXT_MAIN}; }}
        .stMetric label {{ color: {TEXT_MUTED} !important; }}
        section[data-testid="stSidebar"] {{ background-color: {BG_CARD}; }}
        .stProgress > div > div {{ background-color: {INFO}; }}
    </style>
    """, unsafe_allow_html=True)

    st.title("\U0001f40d Hidra Control Plane")

    state = load_state()

    if not state.apps:
        st.warning("Orquestrador nao iniciado ou state.json vazio.")
        st.stop()

    render_kpi_row(state)
    st.divider()
    render_slots(state)
    st.divider()

    tab1, tab2 = st.tabs(["Apps", "Historico"])
    with tab1:
        # Header
        cols = st.columns([3, 1.5, 1, 1, 1.5, 1.5, 1])
        headers = ["App", "Status", "RAM", "CPU", "Hora", "Proximo", ""]
        for col, header in zip(cols, headers):
            with col:
                st.caption(f"**{header}**")
        render_app_table(state)
    with tab2:
        render_history(state)

    # Auto-refresh a cada 5s
    time.sleep(5)
    st.rerun()


if __name__ == "__main__":
    main()
