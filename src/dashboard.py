"""Dashboard Streamlit — painel de controle do Hidra Control Plane."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from state import AppState, ControlPlaneState, load_state, write_command

COMMANDS_DIR = Path(__file__).parent.parent / "commands"
COMMANDS_DIR.mkdir(exist_ok=True)

# ── Cores do dark mode padrão BHub ───────────────────────────
BG_BASE = "#0F172A"
BG_CARD = "#1E293B"
BG_CARD2 = "#263348"
BORDER = "#334155"
TEXT_MAIN = "#F1F5F9"
TEXT_MUTED = "#94A3B8"
TEXT_LABEL = "#CBD5E1"
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
    "paused": WARNING,
}

STATUS_ICONS = {
    "running": "\u25cf",
    "done": "\u2713",
    "off": "\u25cb",
    "queued": "\u23f3",
    "failed": "\u2717",
    "timeout": "\u23f0",
    "paused": "\u23f8",
}


def send_command(action: str, app_name: str = "") -> None:
    """Envia um comando para o orquestrador via arquivo .trigger."""
    write_command(COMMANDS_DIR, action, app_name)


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "\u2014"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    hours = minutes / 60
    return f"{hours:.1f}h"


def format_time(ts: float | None) -> str:
    if not ts:
        return "\u2014"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def render_kpi_row(state: ControlPlaneState) -> None:
    """Renderiza a linha de KPIs no topo."""
    total = len(state.apps)
    running = sum(1 for a in state.apps.values() if a.status == "running")
    failed = sum(1 for a in state.apps.values() if a.status == "failed")
    paused = sum(1 for a in state.apps.values() if a.status == "paused")
    enabled = sum(1 for a in state.apps.values() if a.enabled)

    cols = st.columns(7)
    with cols[0]:
        st.metric("Apps", total)
    with cols[1]:
        st.metric("Ativas", enabled)
    with cols[2]:
        st.metric("Rodando", running)
    with cols[3]:
        st.metric("Pausadas", paused)
    with cols[4]:
        st.metric("Falhas", failed)
    with cols[5]:
        st.metric("RAM VM", f"{state.total_ram_mb / 1024:.1f} GB")
    with cols[6]:
        st.metric("CPU VM", f"{state.total_cpu_pct:.0f}%")


def render_global_controls(state: ControlPlaneState) -> None:
    """Renderiza botões globais Start All / Stop All."""
    any_enabled = any(a.enabled for a in state.apps.values())
    all_enabled = all(a.enabled for a in state.apps.values())

    cols = st.columns([1, 1, 6])
    with cols[0]:
        if st.button(
            "\u25b6 Start All",
            type="primary",
            disabled=all_enabled,
            use_container_width=True,
        ):
            send_command("start_all")
            st.rerun()
    with cols[1]:
        if st.button(
            "\u25a0 Stop All",
            type="secondary",
            disabled=not any_enabled,
            use_container_width=True,
        ):
            send_command("stop_all")
            st.rerun()


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
    """Renderiza a tabela de apps com status e controles."""
    always_apps = {k: v for k, v in state.apps.items() if v.slot == "always"}
    heavy_apps = {k: v for k, v in state.apps.items() if v.slot == "heavy"}
    light_apps = {k: v for k, v in state.apps.items() if v.slot == "light"}

    for section_name, apps in [
        ("\U0001f504 Servicos Always-On", always_apps),
        ("\U0001f4aa Jobs Heavy (1 por vez)", heavy_apps),
        ("\u26a1 Jobs Light (ate 3 paralelos)", light_apps),
    ]:
        if not apps:
            continue
        st.markdown(f"### {section_name}")

        # Header
        cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])
        headers = ["App", "Status", "RAM", "CPU", "Hora", "Proximo", "Controles"]
        for col, header in zip(cols, headers):
            with col:
                st.caption(f"**{header}**")

        for name, app in sorted(apps.items(), key=lambda x: x[1].status != "running"):
            _render_app_row(name, app)


def _render_app_row(name: str, app: AppState) -> None:
    """Renderiza uma linha de app com botões de controle."""
    icon = STATUS_ICONS.get(app.status, "?")
    color = STATUS_COLORS.get(app.status, TEXT_MUTED)

    cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])

    with cols[0]:
        enabled_dot = f"<span style='color:{SUCCESS}'>\u25cf</span>" if app.enabled else f"<span style='color:{TEXT_MUTED}'>\u25cb</span>"
        st.markdown(
            f"{enabled_dot} <span style='color:{color};font-size:1.1em'>{icon}</span> **{name}**",
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.caption(app.status.upper())
    with cols[2]:
        st.caption(f"{app.ram_mb:.0f} MB" if app.ram_mb > 0 else "\u2014")
    with cols[3]:
        st.caption(f"{app.cpu_pct:.0f}%" if app.cpu_pct > 0 else "\u2014")
    with cols[4]:
        if app.status == "running":
            st.caption(format_time(app.started_at))
        else:
            st.caption(format_time(app.finished_at))
    with cols[5]:
        st.caption(app.next_run or "\u2014")
    with cols[6]:
        _render_app_controls(name, app)


def _render_app_controls(name: str, app: AppState) -> None:
    """Renderiza os botões de controle de um app."""
    btn_cols = st.columns(3)

    is_running = app.status in ("running", "queued")
    is_paused = app.status == "paused"
    is_enabled = app.enabled

    with btn_cols[0]:
        # Botão Start — visível quando app está off/done/failed/paused
        if not is_enabled or is_paused:
            if st.button("\u25b6", key=f"start_{name}", help="Iniciar"):
                send_command("start", name)
                st.rerun()

    with btn_cols[1]:
        # Botão Pause — visível quando app está ativo e rodando/agendado
        if is_enabled and not is_paused:
            if st.button("\u23f8", key=f"pause_{name}", help="Pausar"):
                send_command("pause", name)
                st.rerun()

    with btn_cols[2]:
        # Botão Stop — visível quando app está ativo
        if is_enabled or is_running:
            if st.button("\u25a0", key=f"stop_{name}", help="Parar"):
                send_command("stop", name)
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
            f"**{app.name}** \u2014 {app.status} \u2014 {duration} \u2014 {finished}"
            + (f" \u2014 `{app.last_error[:80]}`" if app.last_error else ""),
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
        /* Botões de controle compactos */
        div[data-testid="column"] button {{
            padding: 0.2rem 0.5rem;
            min-height: 0;
        }}
    </style>
    """, unsafe_allow_html=True)

    st.title("\U0001f40d Hidra Control Plane")

    state = load_state()

    if not state.apps:
        st.warning("Orquestrador nao iniciado ou state.json vazio. Inicie o main.py primeiro.")
        st.stop()

    render_kpi_row(state)
    st.divider()

    render_global_controls(state)
    st.divider()

    render_slots(state)
    st.divider()

    tab1, tab2 = st.tabs(["Apps", "Historico"])
    with tab1:
        render_app_table(state)
    with tab2:
        render_history(state)

    # Auto-refresh a cada 5s
    time.sleep(5)
    st.rerun()


if __name__ == "__main__":
    main()
