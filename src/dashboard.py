"""Dashboard Streamlit — painel administrativo do Hidra Control Plane."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from app_discovery import APPS_DIR, scan_apps_dir
from config_writer import read_config_raw, upsert_app
from executable_detector import build_command
from execution_logger import (
    get_latest_log_path,
    list_apps_with_logs,
    read_history,
    read_log_content,
)
from state import AppState, ControlPlaneState, load_state, write_command

ROOT = Path(__file__).parent.parent
COMMANDS_DIR = ROOT / "commands"
CONFIG_PATH = ROOT / "config.yaml"
COMMANDS_DIR.mkdir(exist_ok=True)

# ── Cores do tema LIGHT ──────────────────────────────────────
BG_BASE = "#F8FAFC"     # fundo principal (branco suave)
BG_CARD = "#FFFFFF"     # cards
BG_CARD2 = "#F1F5F9"    # cabeçalhos/linhas alternadas
BORDER = "#E2E8F0"      # bordas
TEXT_MAIN = "#0F172A"   # texto principal (quase preto)
TEXT_MUTED = "#64748B"  # texto secundário (cinza médio)
TEXT_LABEL = "#475569"  # labels
SUCCESS = "#059669"     # verde (emerald 600)
ERROR = "#DC2626"       # vermelho (red 600)
WARNING = "#D97706"     # âmbar (amber 600)
INFO = "#2563EB"        # azul (blue 600)

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
    "running": "●",
    "done": "✓",
    "off": "○",
    "queued": "⏳",
    "failed": "✗",
    "timeout": "⏰",
    "paused": "⏸",
}

SCHEDULE_LABELS = {
    "manual": "Manual (roda uma vez ao ativar)",
    "loop": "Repetir com tempo entre rodagens",
}


def send_command(action: str, app_name: str = "") -> None:
    write_command(COMMANDS_DIR, action, app_name)


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


# ═══════════════════════════════════════════════════════════════
# ABA: STATUS (dashboard operacional)
# ═══════════════════════════════════════════════════════════════

def render_kpi_row(state: ControlPlaneState) -> None:
    total = len(state.apps)
    running = sum(1 for a in state.apps.values() if a.status == "running")
    failed = sum(1 for a in state.apps.values() if a.status == "failed")
    enabled = sum(1 for a in state.apps.values() if a.enabled)
    waiting_mem = len(state.memory_queue)

    cols = st.columns(8)
    with cols[0]:
        st.metric("Apps", total)
    with cols[1]:
        st.metric("Ativas", enabled)
    with cols[2]:
        st.metric("Rodando", running)
    with cols[3]:
        st.metric("Fila RAM", waiting_mem)
    with cols[4]:
        st.metric("Falhas", failed)
    with cols[5]:
        st.metric("RAM usada", f"{state.total_ram_mb / 1024:.1f} GB")
    with cols[6]:
        st.metric("RAM livre", f"{state.available_ram_mb / 1024:.1f} GB")
    with cols[7]:
        st.metric("CPU VM", f"{state.total_cpu_pct:.0f}%")


def render_global_controls(state: ControlPlaneState) -> None:
    any_enabled = any(a.enabled for a in state.apps.values())
    all_enabled = all(a.enabled for a in state.apps.values()) if state.apps else False

    cols = st.columns([1, 1, 6])
    with cols[0]:
        if st.button("▶ Start All", type="primary", disabled=all_enabled, use_container_width=True):
            send_command("start_all")
            st.rerun()
    with cols[1]:
        if st.button("■ Stop All", type="secondary", disabled=not any_enabled, use_container_width=True):
            send_command("stop_all")
            st.rerun()


def render_slots(state: ControlPlaneState) -> None:
    c1, c2 = st.columns(2)
    with c1:
        used = state.heavy_slots_used
        total = state.heavy_slots_max
        st.markdown(f"**Slot Heavy:** {used}/{total}")
        st.progress((used / total) if total else 0)
    with c2:
        used = state.light_slots_used
        total = state.light_slots_max
        st.markdown(f"**Slot Light:** {used}/{total}")
        st.progress((used / total) if total else 0)


def render_app_table(state: ControlPlaneState) -> None:
    always_apps = {k: v for k, v in state.apps.items() if v.slot == "always"}
    heavy_apps = {k: v for k, v in state.apps.items() if v.slot == "heavy"}
    light_apps = {k: v for k, v in state.apps.items() if v.slot == "light"}

    for section_name, apps in [
        ("\U0001f504 Servicos Always-On", always_apps),
        ("\U0001f4aa Jobs Heavy (1 por vez)", heavy_apps),
        ("⚡ Jobs Light (ate 3 paralelos)", light_apps),
    ]:
        if not apps:
            continue
        st.markdown(f"### {section_name}")
        cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])
        headers = ["App", "Status", "RAM", "CPU", "Hora", "Proximo", "Controles"]
        for col, header in zip(cols, headers):
            with col:
                st.caption(f"**{header}**")
        for name, app in sorted(apps.items(), key=lambda x: x[1].status != "running"):
            _render_app_row(name, app)


def _render_app_row(name: str, app: AppState) -> None:
    icon = STATUS_ICONS.get(app.status, "?")
    color = STATUS_COLORS.get(app.status, TEXT_MUTED)

    cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])

    with cols[0]:
        enabled_dot = (
            f"<span style='color:{SUCCESS}'>●</span>"
            if app.enabled
            else f"<span style='color:{TEXT_MUTED}'>○</span>"
        )
        st.markdown(
            f"{enabled_dot} <span style='color:{color};font-size:1.1em'>{icon}</span> **{name}**",
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.caption(app.status.upper())
    with cols[2]:
        st.caption(f"{app.ram_mb:.0f} MB" if app.ram_mb > 0 else "—")
    with cols[3]:
        st.caption(f"{app.cpu_pct:.0f}%" if app.cpu_pct > 0 else "—")
    with cols[4]:
        if app.status == "running":
            st.caption(format_time(app.started_at))
        else:
            st.caption(format_time(app.finished_at))
    with cols[5]:
        st.caption(app.next_run or "—")
    with cols[6]:
        _render_app_controls(name, app)


def _render_app_controls(name: str, app: AppState) -> None:
    btn_cols = st.columns(3)
    is_running = app.status in ("running", "queued")
    is_paused = app.status == "paused"
    is_enabled = app.enabled

    with btn_cols[0]:
        if not is_enabled or is_paused:
            if st.button("▶", key=f"start_{name}", help="Iniciar"):
                send_command("start", name)
                st.rerun()
    with btn_cols[1]:
        if is_enabled and not is_paused:
            if st.button("⏸", key=f"pause_{name}", help="Pausar"):
                send_command("pause", name)
                st.rerun()
    with btn_cols[2]:
        if is_enabled or is_running:
            if st.button("■", key=f"stop_{name}", help="Parar"):
                send_command("stop", name)
                st.rerun()


# ═══════════════════════════════════════════════════════════════
# ABA: FILA
# ═══════════════════════════════════════════════════════════════

def render_queue_view(state: ControlPlaneState) -> None:
    st.markdown("### \U0001f4cb Filas de execução")

    # Banner de RAM disponível
    available_gb = state.available_ram_mb / 1024
    safety_mb = state.ram_safety_margin_mb
    usable_mb = max(0.0, state.available_ram_mb - safety_mb)
    st.markdown(
        f"**RAM disponível:** {available_gb:.2f} GB "
        f"(**{usable_mb:.0f} MB utilizáveis** após margem de {safety_mb} MB para o SO)"
    )

    # Filas de slot
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Slot Heavy** ({state.heavy_slots_used}/{state.heavy_slots_max} em uso)")
        if not state.heavy_queue:
            st.info("Fila vazia")
        else:
            for pos, app_name in enumerate(state.heavy_queue, 1):
                st.markdown(f"`{pos}.` **{app_name}** ⏳ aguardando slot")
    with col2:
        st.markdown(f"**Slot Light** ({state.light_slots_used}/{state.light_slots_max} em uso)")
        if not state.light_queue:
            st.info("Fila vazia")
        else:
            for pos, app_name in enumerate(state.light_queue, 1):
                st.markdown(f"`{pos}.` **{app_name}** ⏳ aguardando slot")

    # Fila de memória
    st.divider()
    st.markdown("### \U0001f9e0 Fila de memória")
    st.caption(
        "Apps que já conquistaram o slot mas estão aguardando RAM suficiente "
        "para iniciar com segurança."
    )
    if not state.memory_queue:
        st.info("Nenhum app aguardando memória")
    else:
        for pos, app_name in enumerate(state.memory_queue, 1):
            app = state.apps.get(app_name)
            next_info = f" — {app.next_run}" if app and app.next_run else ""
            st.markdown(
                f"<span style='color:{WARNING}'>⏳</span> "
                f"`{pos}.` **{app_name}**{next_info}",
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("### \U0001f3ac Apps rodando agora")
    running = [a for a in state.apps.values() if a.status == "running"]
    if not running:
        st.info("Nenhum app em execução")
    else:
        for app in running:
            st.markdown(
                f"<span style='color:{SUCCESS}'>●</span> **{app.name}** "
                f"— PID {app.pid} — {app.ram_mb:.0f} MB — "
                f"iniciado {format_time(app.started_at)}",
                unsafe_allow_html=True,
            )


# ═══════════════════════════════════════════════════════════════
# ABA: CONFIGURAR (CRUD de apps)
# ═══════════════════════════════════════════════════════════════

def render_config_tab(state: ControlPlaneState) -> None:
    st.markdown("### ⚙️ Apps da pasta `apps_executaveis/`")
    st.caption(
        f"Solte arquivos `.vbs`, `.exe`, `.bat`, `.ps1`, `.py` ou `.lnk` em "
        f"`{APPS_DIR}` e eles aparecem aqui automaticamente. "
        f"Marque ✓ para ativar, defina o tempo entre rodagens e pronto."
    )

    c_scan, _ = st.columns([1, 5])
    with c_scan:
        if st.button("\U0001f504 Rescanear pasta", use_container_width=True):
            st.rerun()

    discovered = scan_apps_dir()
    raw = read_config_raw(CONFIG_PATH)
    apps = raw.get("apps", {})

    if not discovered:
        st.warning(
            f"Nenhum arquivo executável na pasta.\n\n"
            f"Cole `.vbs` / `.exe` / `.bat` / `.ps1` / `.py` / `.lnk` em:\n"
            f"`{APPS_DIR}`"
        )
        return

    st.markdown(f"**{len(discovered)} arquivo(s) encontrado(s):**")
    for app in discovered:
        existing = apps.get(app.name, {})
        _render_pasta_row(app, existing, state)


def _render_pasta_row(app, existing: dict, state: ControlPlaneState) -> None:
    """Linha inline para um app descoberto na pasta apps_executaveis/."""
    is_cadastrado = bool(existing)
    app_state = state.apps.get(app.name)

    # Status live
    live_badge = ""
    if app_state:
        color = STATUS_COLORS.get(app_state.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(app_state.status, "?")
        live_badge = (
            f"<span style='color:{color};font-size:1.1em'>{icon}</span> "
            f"<span style='color:{TEXT_MUTED}'>{app_state.status}</span>"
        )

    # Slot atual
    current_slot = existing.get("slot", "light")
    current_schedule = existing.get("schedule", "manual")
    current_pause = int(existing.get("pause_between", 600))
    current_ram = int(existing.get("max_ram_mb", 512))
    current_auto = bool(existing.get("auto_start", False))
    is_enabled_in_config = current_schedule == "loop" or current_auto

    with st.container():
        st.markdown(
            f"#### {app.icon} **{app.name}** "
            f"<span style='color:{TEXT_MUTED};font-weight:normal'>"
            f"({app.info.display_kind})</span> {live_badge}",
            unsafe_allow_html=True,
        )
        st.caption(f"📄 `{app.file_path}`")

        c1, c2, c3, c4, c5 = st.columns([1.2, 2, 1.2, 1.2, 1.2])

        # 1) Checkbox: Ativar rodagem periódica
        with c1:
            enabled = st.checkbox(
                "Executar",
                value=is_enabled_in_config,
                key=f"enable_{app.name}",
                help=(
                    "Marcado: app roda a cada 'tempo entre rodagens'. "
                    "Desmarcado: fica disponível para disparo manual na aba Status."
                ),
            )

        # 2) Tempo entre rodagens com unidade
        with c2:
            if current_pause >= 3600 and current_pause % 3600 == 0:
                default_unit = "horas"
                default_val = current_pause // 3600
            elif current_pause >= 60 and current_pause % 60 == 0:
                default_unit = "minutos"
                default_val = current_pause // 60
            else:
                default_unit = "segundos"
                default_val = current_pause

            tc1, tc2 = st.columns([1, 1])
            with tc1:
                val = st.number_input(
                    "Tempo entre rodagens",
                    min_value=1,
                    max_value=86400,
                    value=int(default_val),
                    key=f"pause_v_{app.name}",
                )
            with tc2:
                unit = st.selectbox(
                    "Unidade",
                    ["segundos", "minutos", "horas"],
                    index=["segundos", "minutos", "horas"].index(default_unit),
                    key=f"pause_u_{app.name}",
                    label_visibility="visible",
                )
            multiplier = {"segundos": 1, "minutos": 60, "horas": 3600}[unit]
            pause_seconds = int(val) * multiplier

        # 3) Slot
        with c3:
            slot = st.selectbox(
                "Slot",
                ["heavy", "light", "always"],
                index=["heavy", "light", "always"].index(current_slot),
                key=f"slot_{app.name}",
                help="heavy = 1 por vez | light = até 3 paralelos | always = permanente",
            )

        # 4) RAM máxima
        with c4:
            ram = st.number_input(
                "RAM máx (MB)",
                min_value=64,
                max_value=16384,
                value=current_ram,
                step=64,
                key=f"ram_{app.name}",
            )

        # 5) Botão Salvar
        with c5:
            st.write("")
            st.write("")
            save_clicked = st.button(
                "\U0001f4be Salvar",
                key=f"save_{app.name}",
                type="primary",
                use_container_width=True,
            )

        if save_clicked:
            cmd, cwd = build_command(str(app.file_path))
            app_data = {
                "slot": slot,
                "cwd": cwd,
                "cmd": cmd,
                "schedule": "loop" if enabled and slot != "always" else "manual",
                "max_ram_mb": int(ram),
                "timeout": int(existing.get("timeout", 600)),
                "_source": "pasta",
            }
            if enabled and slot != "always":
                app_data["pause_between"] = pause_seconds
            if enabled and slot == "always":
                app_data["auto_start"] = True
            if existing.get("restart_on_crash"):
                app_data["restart_on_crash"] = True

            upsert_app(CONFIG_PATH, app.name, app_data)
            st.success(
                f"✅ '{app.name}' salvo — hot reload em até 5s"
                + (" (vai começar a rodar)" if enabled else " (ficou em modo manual)")
            )
            st.rerun()

        # Botões Start/Stop inline (atalhos pra aba Status)
        if app_state and is_cadastrado:
            bc1, bc2, _ = st.columns([1, 1, 6])
            with bc1:
                if not app_state.enabled:
                    if st.button(
                        "▶ Start agora", key=f"instart_{app.name}",
                        use_container_width=True,
                    ):
                        send_command("start", app.name)
                        st.rerun()
            with bc2:
                if app_state.enabled:
                    if st.button(
                        "■ Stop", key=f"instop_{app.name}",
                        use_container_width=True,
                    ):
                        send_command("stop", app.name)
                        st.rerun()

        st.divider()






# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def render_live_view(state: ControlPlaneState) -> None:
    """Aba ao vivo — stream do log da última execução do app selecionado."""
    st.markdown("### 📺 Execução ao vivo")

    apps_with_logs = list_apps_with_logs()
    # Priorizar apps em execução no topo do seletor
    running_apps = [name for name, a in state.apps.items() if a.status == "running"]
    other_apps = [a for a in apps_with_logs if a not in running_apps]
    options = running_apps + other_apps

    if not options:
        st.info("Nenhuma execução registrada ainda. Inicie um app para ver logs aqui.")
        return

    format_option = lambda name: (
        f"🟢 {name} (rodando)" if name in running_apps else f"⚪ {name}"
    )

    selected = st.selectbox(
        "App para monitorar",
        options=options,
        format_func=format_option,
        key="live_app_select",
    )

    if not selected:
        return

    log_path = get_latest_log_path(selected)
    if not log_path:
        st.info(f"Sem logs para '{selected}' ainda.")
        return

    # Metadados da última execução
    recs = read_history(selected, limit=1)
    if recs:
        r = recs[0]
        status_color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        cols = st.columns(4)
        with cols[0]:
            st.markdown(
                f"**Status:** <span style='color:{status_color}'>{icon} {r.status}</span>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            st.markdown(f"**Início:** {r.started_at_str}")
        with cols[2]:
            st.markdown(f"**Duração:** {format_duration(r.duration_s)}")
        with cols[3]:
            st.markdown(
                f"**Pico RAM:** {r.peak_ram_mb:.0f} MB"
                if r.peak_ram_mb > 0
                else "**Pico RAM:** —"
            )

    st.caption(f"📄 `{log_path}`")

    # Stream do log (últimos 64 KB)
    content = read_log_content(log_path, tail_kb=64)
    st.code(content, language=None, line_numbers=False)


def render_history_rich(_state: ControlPlaneState) -> None:
    """Histórico detalhado por app com drill-down em cada execução."""
    st.markdown("### \U0001f4dc Histórico detalhado")

    apps_with_logs = list_apps_with_logs()
    if not apps_with_logs:
        st.info("Nenhuma execução registrada ainda.")
        return

    # Resumo rápido: última execução de cada app
    st.markdown("#### Última execução por app")
    for app_name in apps_with_logs:
        recs = read_history(app_name, limit=1)
        if not recs:
            continue
        r = recs[0]
        color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        st.markdown(
            f"<span style='color:{color}'>{icon}</span> "
            f"**{app_name}** — {r.status} — {format_duration(r.duration_s)} — "
            f"{r.started_at_str}"
            + (f" — `{r.error[:80]}`" if r.error else ""),
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("#### Drill-down")
    selected = st.selectbox(
        "Ver histórico completo de:",
        options=apps_with_logs,
        key="history_app_select",
    )
    if not selected:
        return

    recs = read_history(selected, limit=50)
    if not recs:
        st.info(f"Sem histórico para '{selected}'.")
        return

    st.caption(f"Últimas {len(recs)} execuções de **{selected}**")
    for r in recs:
        color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        label = (
            f"{icon} {r.started_at_str} — {r.status} — "
            f"{format_duration(r.duration_s)}"
            + (f" (exit {r.exit_code})" if r.exit_code not in (None, 0) else "")
        )
        with st.expander(label):
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown(f"**exec_id:** `{r.exec_id}`")
            with c2:
                st.markdown(f"**PID:** {r.pid or '—'}")
            with c3:
                st.markdown(f"**Pico RAM:** {r.peak_ram_mb:.0f} MB")
            with c4:
                st.markdown(f"**Exit:** {r.exit_code}")
            if r.error:
                st.error(r.error)
            if r.log_file and Path(r.log_file).exists():
                st.markdown("**Log completo:**")
                st.code(read_log_content(r.log_file, tail_kb=128))
            else:
                st.caption("(arquivo de log não disponível)")


def main() -> None:
    st.set_page_config(
        page_title="Hidra Control Plane",
        page_icon="\U0001f40d",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # CSS mínimo — o tema light já vem do .streamlit/config.toml
    st.markdown(f"""
    <style>
        div[data-testid="column"] button {{
            padding: 0.2rem 0.5rem;
            min-height: 0;
        }}
        .stProgress > div > div {{ background-color: {INFO}; }}
    </style>
    """, unsafe_allow_html=True)

    st.title("\U0001f40d Hidra Control Plane")

    state = load_state()

    tabs = st.tabs([
        "\U0001f4ca Status",
        "\U0001f4fa Ao vivo",
        "\U0001f4cb Fila",
        "⚙️ Configurar",
        "\U0001f4dc Histórico",
    ])

    with tabs[0]:
        if not state.apps:
            st.warning("Orquestrador não iniciado ou sem apps configurados.")
        else:
            render_kpi_row(state)
            st.divider()
            render_global_controls(state)
            st.divider()
            render_slots(state)
            st.divider()
            render_app_table(state)

    with tabs[1]:
        render_live_view(state)

    with tabs[2]:
        if not state.apps:
            st.warning("Orquestrador não iniciado.")
        else:
            render_queue_view(state)

    with tabs[3]:
        render_config_tab(state)

    with tabs[4]:
        render_history_rich(state)

    # Auto-refresh a cada 5s apenas nas abas operacionais
    time.sleep(5)
    st.rerun()


if __name__ == "__main__":
    main()
